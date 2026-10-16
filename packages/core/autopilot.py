"""
AUTOPILOT -- the conductor of the acquisition workforce.

It does not do the work (the 17 agents do) and it does not decide who may be emailed (the qualification gate and
the outbound governor do). Its job is to keep the pipeline moving and the founder informed:

    discover -> [ingest gate] -> research -> [final gate] -> draft -> (approval) -> governed send
             -> reply monitoring -> follow-up (once) -> hot-lead escalation -> reports

Each `tick` looks at what is actually in the database and creates only the next tasks that are justified:
  * research for prospects that cleared ingest;
  * drafts ONLY for prospects that passed the full gate -- there is no quota: with 8 qualified prospects, 8 drafts;
  * more discovery ONLY when the qualified stock is running low (and it backs off / rotates geography when a
    source runs dry, telling the founder instead of lowering standards);
  * the single follow-up for prospects that stayed silent;
  * escalations for hot leads, stale approvals and breaker trips, de-duplicated so the founder is not spammed.

Two modes. `approval` (default): the founder approves every outbound email. `auto`: emails go out without a
per-email approval -- available only after the founder has approved enough drafts to validate the system's
judgement (and with explicit acknowledgement); every governor limit still applies in both modes.
The switch, the mode and the breaker live in `autopilot_state`, which agent code cannot read or write.
"""
from __future__ import annotations

import logging
import math
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func

from packages.core import campaign, outbound
from packages.core import qualification as qual
from packages.core.autopilot_config import AutopilotConfig, ConfigError
from packages.core.config import Settings, get_settings
from packages.core.context import Context
from packages.core.domains import DomainChecker, check_sender_authentication, get_domain_checker
from packages.core.incidents import IncidentService
from packages.core.log import get_logger, log
from packages.core.services import TaskService
from packages.db.models import (
    Agent, Approval, AutopilotState, Incident, Product, ProductConfig, ProductConfigHistory, Prospect, Task,
)
from packages.db.session import session_scope

_log = get_logger("autopilot")
OPEN_TASK_STATUSES = ("pending", "in_progress", "needs_approval")
REDRAFT_COOLDOWN_DAYS = 2
MAX_RESEARCH_PER_TICK = 15
FOLLOW_UP_CHECK_HOURS = 6


class AutopilotError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ====================================================================== readiness
def _check(name: str, ok: bool, detail: str, blocking: bool = True) -> dict:
    return {"name": name, "ok": ok, "blocking": blocking and not ok, "detail": detail}


def readiness(company_id: uuid.UUID, product_id: uuid.UUID, *, settings: Optional[Settings] = None,
              checker: Optional[DomainChecker] = None) -> dict:
    """Everything that must be true before real email may be sent -- and an honest list of what is not."""
    from packages.core.workers import workers_health
    settings = settings or get_settings()
    live_send = settings.outreach_mode == "smtp"
    with session_scope() as session:
        config = session.get(ProductConfig, product_id)
        state = session.get(AutopilotState, product_id)
        icp, positioning, sales = (config.icp if config else None) or {}, (config.positioning if config else None) or {}, (config.sales if config else None) or {}
        cfg_error, cfg = None, None
        try:
            cfg = load_cfg(state)
        except ConfigError as exc:
            cfg_error = str(exc)

    checks = []
    policy = qual.QualificationPolicy.from_icp(icp)
    has_source = bool(icp.get("industries") or icp.get("osm_tags"))
    has_geo = bool(icp.get("geography") or icp.get("geographies"))
    checks.append(_check("icp_target", has_source and has_geo, "industries (or osm_tags) and geography are set" if has_source and has_geo
                         else "icp needs `industries` (or `osm_tags`) AND `geography`/`geographies` -- who to look for, and where"))
    checks.append(_check("qualification_policy", policy is not None, "icp.qualification policy is set" if policy else
                         "icp.qualification is missing -- the hard gate that decides who may be contacted"))
    missing = [k for k in ("product_name", "one_liner") if not str(positioning.get(k) or "").strip()]
    checks.append(_check("positioning", not missing, "product_name and one_liner are set" if not missing else f"positioning is missing: {', '.join(missing)}"))
    checks.append(_check("physical_address", bool(sales.get("physical_address")), "postal address is set (CAN-SPAM)" if sales.get("physical_address")
                         else "sales.physical_address is required in every commercial email (CAN-SPAM)"))
    checks.append(_check("sender_name", bool(sales.get("sender_name")), "sales.sender_name is set" if sales.get("sender_name")
                         else "sales.sender_name (who the email is truthfully from) is not set", blocking=False))
    checks.append(_check("autopilot_config", cfg_error is None, "valid" if cfg_error is None else cfg_error))
    goal = campaign.progress(product_id)
    goal_ok = goal["status"] in ("pending", "active")
    checks.append(_check("campaign_goal", goal_ok, goal["message"] if goal_ok else (
        "no goal set -- tell the system what success is: autopilot_ctl goal set --customers N --days D" if goal["status"] == "unset"
        else f"the campaign is {goal['status']} -- extend it or set a new goal: autopilot_ctl goal extend --days N | goal set ...")))

    smtp_ok = bool(settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.smtp_from_email)
    if live_send:
        checks.append(_check("smtp_credentials", smtp_ok, "SMTP mailbox configured" if smtp_ok else "OUTREACH_MODE=smtp but SMTP_* credentials are incomplete"))
        imap_ok = bool(settings.imap_host and (settings.imap_username or settings.smtp_username) and (settings.imap_password or settings.smtp_password))
        checks.append(_check("inbound_configured", imap_ok, "IMAP configured -- replies, unsubscribes and bounces will be read" if imap_ok
                             else "IMAP is not configured: unsubscribes and bounces could not be processed, so live sending is blocked"))
        if smtp_ok:
            auth = check_sender_authentication(settings.smtp_from_email, checker or get_domain_checker())
            # Unknown (DNS lookup failed) blocks too: we only start live sending once SPF is actually confirmed. A transient
            # DNS failure just means "try again in a minute".
            checks.append(_check("sender_spf", auth["spf"] is True, f"SPF found for {auth['domain']}" if auth["spf"] is True else
                                 (f"no SPF record on {auth['domain']} -- mail will land in spam" if auth["spf"] is False
                                  else f"could not verify SPF for {auth['domain']} (DNS lookup failed) -- check connectivity and retry"),
                                 blocking=auth["spf"] is not True))
            checks.append(_check("sender_dmarc", auth["dmarc"] is True, "DMARC found" if auth["dmarc"] is True else
                                 f"no DMARC record on {auth['domain']} (strongly recommended)", blocking=False))
    else:
        checks.append(_check("dry_run", True, "OUTREACH_MODE=stub: DRY RUN -- drafts and approvals work, nothing is actually emailed", blocking=False))
    checks.append(_check("enrichment_live", settings.enrichment_mode == "free", "free discovery + research enabled" if settings.enrichment_mode == "free"
                         else "ENRICHMENT_MODE=stub: no real prospects will be found or researched", blocking=False))
    checks.append(_check("llm", settings.has_live_llm, "Claude writes and classifies" if settings.has_live_llm else
                         "no ANTHROPIC_API_KEY: deterministic templates and keyword reply-classification are used", blocking=False))
    checks.append(_check("notifications", settings.has_live_notifications, "Slack alerts on" if settings.has_live_notifications
                         else "no SLACK_WEBHOOK_URL: alerts only reach the console", blocking=False))
    checks.append(_check("report_delivery", bool(settings.report_email_to) or settings.has_live_notifications, "growth reports will be delivered" if (settings.report_email_to or settings.has_live_notifications)
                         else "no REPORT_EMAIL_TO or Slack: reports are stored but nobody is told", blocking=False))
    health = workers_health(settings.worker_stale_after_seconds)
    checks.append(_check("worker_running", health["workers_alive"] > 0, f"{health['workers_alive']} worker(s) alive" if health["workers_alive"] else
                         "no worker process is running: tasks will queue but not execute", blocking=False))
    checks.append(_check("inbound_running", health["inbound_alive"] > 0 or not live_send,
                         "inbound checker alive" if health["inbound_alive"] else
                         ("not needed in dry run" if not live_send else "no inbound checker process is running: replies, unsubscribes and bounces are not being read"),
                         blocking=False))

    return {"ready": not any(c["blocking"] for c in checks), "dry_run": not live_send, "checks": checks,
            "blocking": [c["name"] for c in checks if c["blocking"]], "evaluated_at": _now().isoformat()}


def load_cfg(state: Optional[AutopilotState]) -> AutopilotConfig:
    return outbound.load_config(state)


# ====================================================================== controls
def get_status(company_id: uuid.UUID, product_id: uuid.UUID) -> dict:
    now = _now()
    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        cfg = load_cfg(state)
        stages = dict(session.query(Prospect.stage, func.count()).filter_by(product_id=product_id).group_by(Prospect.stage).all())
        pending_approvals = _pending_approval_count(session, product_id)
        info = {
            "enabled": bool(state and state.enabled), "mode": state.mode if state else "approval",
            "paused": state.paused_reason if state else None, "paused_at": state.paused_at.isoformat() if state and state.paused_at else None,
            "last_tick_at": state.last_tick_at.isoformat() if state and state.last_tick_at else None,
            "config": cfg.to_dict(), "cursor": dict(state.cursor or {}) if state else {},
        }
    goal = campaign.progress(product_id)
    if info["enabled"] and not info["paused"] and goal["status"] in ("completed", "expired"):
        info["effective_state"] = None          # filled below
    return {**info, "goal": goal, "pipeline": stages, "pending_approvals": pending_approvals,
            "capacity": outbound.capacity_snapshot(company_id, product_id, now),
            "effective_state": (f"ON, but the campaign is {goal['status']}: no new outreach is planned" if info.get("effective_state") is None and "effective_state" in info else _effective(info))}


def _effective(info: dict) -> str:
    if info["paused"]:
        return "PAUSED by safety breaker"
    if not info["enabled"]:
        return "OFF"
    return "ON (founder approves every email)" if info["mode"] == "approval" else "ON (auto-send, no per-email approval)"


def _founder_track_record(session, company_id: uuid.UUID) -> dict:
    """The founder's verdicts on THIS company's outreach drafts -- the evidence that the system's judgement is trusted."""
    def count(status: str) -> int:
        return session.query(func.count(Approval.id)).join(Task, Task.id == Approval.task_id).filter(
            Task.company_id == company_id, Approval.requested_by == "sales", Approval.status == status).scalar() or 0
    approved, rejected = count("approved"), count("rejected")
    return {"approved": approved, "rejected": rejected, "reject_rate": (rejected / (approved + rejected)) if (approved + rejected) else 0.0}


def enable(company_id: uuid.UUID, product_id: uuid.UUID, *, mode: str = "approval", by: str = "founder",
           ack_auto: bool = False, force_dry_run_ok: bool = True, checker: Optional[DomainChecker] = None) -> dict:
    if mode not in ("approval", "auto"):
        raise AutopilotError("mode must be 'approval' or 'auto'")
    report = readiness(company_id, product_id, checker=checker)
    if not report["ready"]:
        raise AutopilotError("not ready to start: " + "; ".join(c["detail"] for c in report["checks"] if c["blocking"]))
    with session_scope() as session:
        state = outbound.ensure_state(session, product_id)
        cfg = load_cfg(state)
        if mode == "auto":
            record = _founder_track_record(session, company_id)
            if not ack_auto:
                raise AutopilotError("auto mode sends without per-email approval: pass the explicit acknowledgement (--ack-auto)")
            if report["dry_run"]:
                raise AutopilotError("auto mode is only meaningful with live sending (OUTREACH_MODE=smtp)")
            if record["approved"] < cfg.min_founder_approvals_for_auto:
                raise AutopilotError(f"auto mode unlocks after you have approved {cfg.min_founder_approvals_for_auto} drafts yourself "
                                     f"(so the system's judgement is validated); you have approved {record['approved']}")
            if record["reject_rate"] > 0.2:
                raise AutopilotError(f"you rejected {record['reject_rate']:.0%} of drafts -- the copy/targeting is not trusted enough for auto mode yet")
        state.enabled, state.mode, state.updated_by, state.readiness = True, mode, by, report
        campaign.activate_if_pending(session, state)                 # "N customers in D days" starts counting now
        product = session.get(Product, product_id)
        if product is not None and product.lifecycle == "draft":
            product.lifecycle = "active"                       # live products get funnel-degradation monitoring
    log(_log, logging.WARNING, "autopilot enabled", mode=mode, by=by, dry_run=report["dry_run"], product_id=str(product_id))
    return get_status(company_id, product_id)


def disable(company_id: uuid.UUID, product_id: uuid.UUID, by: str = "founder") -> dict:
    with session_scope() as session:
        state = outbound.ensure_state(session, product_id)
        state.enabled, state.updated_by = False, by
    log(_log, logging.WARNING, "autopilot disabled", by=by, product_id=str(product_id))
    return get_status(company_id, product_id)


def set_mode(company_id: uuid.UUID, product_id: uuid.UUID, mode: str, *, ack_auto: bool = False) -> dict:
    """Downgrading to approval mode is always allowed instantly; upgrading re-runs the full enable() checks."""
    if mode == "approval":
        with session_scope() as session:
            outbound.ensure_state(session, product_id).mode = "approval"
        return get_status(company_id, product_id)
    return enable(company_id, product_id, mode=mode, ack_auto=ack_auto)


def update_config(company_id: uuid.UUID, product_id: uuid.UUID, patch: dict) -> dict:
    with session_scope() as session:
        state = outbound.ensure_state(session, product_id)
        merged = {**load_cfg(state).to_dict(), **patch}
        AutopilotConfig.from_dict(merged)                       # validates BEFORE persisting; raises ConfigError
        state.config = {k: v for k, v in merged.items()}
    return get_status(company_id, product_id)


def auto_send_allowed(company_id: uuid.UUID, product_id: uuid.UUID) -> bool:
    """The Sales agent re-verifies this itself at send time; a task labelled 'auto' proves nothing."""
    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        if not state or not state.enabled or state.mode != "auto" or state.paused_reason:
            return False
        record = _founder_track_record(session, company_id)
        return record["approved"] >= load_cfg(state).min_founder_approvals_for_auto and record["reject_rate"] <= 0.2


# ====================================================================== product configuration
def validate_acquisition_config(icp: Optional[dict], positioning: Optional[dict], sales: Optional[dict]) -> list[str]:
    problems = []
    icp, positioning, sales = icp or {}, positioning or {}, sales or {}
    if not (icp.get("industries") or icp.get("osm_tags")):
        problems.append("icp.industries (or icp.osm_tags) is required")
    if not (icp.get("geography") or icp.get("geographies")):
        problems.append("icp.geography (or icp.geographies) is required")
    if not isinstance(icp.get("qualification"), dict):
        problems.append("icp.qualification block is required (the hard gate)")
    else:
        known = set(qual.QualificationPolicy.__dataclass_fields__) | {"llm_fit_prompt"}
        unknown = set(icp["qualification"]) - known
        if unknown:
            problems.append(f"unknown icp.qualification setting(s): {sorted(unknown)}")
        try:
            if not 0 <= float(icp["qualification"].get("min_score", 0.6)) <= 1:
                problems.append("icp.qualification.min_score must be between 0 and 1")
        except (TypeError, ValueError):
            problems.append("icp.qualification.min_score must be a number")
    for key in ("product_name", "one_liner"):
        if not str(positioning.get(key) or "").strip():
            problems.append(f"positioning.{key} is required")
    if not sales.get("physical_address"):
        problems.append("sales.physical_address is required (CAN-SPAM)")

    def placeholders(node, path=""):
        if isinstance(node, str) and "REPLACE_ME" in node:
            yield path
        elif isinstance(node, dict):
            for k, v in node.items():
                yield from placeholders(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from placeholders(v, f"{path}[{i}]")
    for where in placeholders({"icp": icp, "positioning": positioning, "sales": sales}):
        problems.append(f"{where} still contains REPLACE_ME -- fill in the real value")
    return problems


def apply_product_config(product_id: uuid.UUID, *, icp=None, positioning=None, sales=None, pricing=None, competitors=None,
                         changed_by: str = "founder") -> int:
    """Writes the founder's configuration, versioned (product_config_history keeps the previous snapshot)."""
    problems = validate_acquisition_config(icp, positioning, sales)
    if problems:
        raise ConfigError("; ".join(problems))
    with session_scope() as session:
        config = session.get(ProductConfig, product_id)
        if config is None:
            raise ConfigError(f"product {product_id} has no config row")
        session.add(ProductConfigHistory(product_id=product_id, config_version=config.config_version, changed_by=changed_by,
                                         snapshot={"icp": config.icp, "positioning": config.positioning, "sales": config.sales,
                                                   "pricing": config.pricing, "competitors": config.competitors}))
        config.icp, config.positioning, config.sales = icp, positioning, sales
        if pricing is not None:
            config.pricing = pricing
        if competitors is not None:
            config.competitors = competitors
        config.config_version += 1
        return config.config_version


# ====================================================================== the tick
def _pending_approval_count(session, product_id: uuid.UUID) -> int:
    return session.query(func.count(Approval.id)).join(Task, Task.id == Approval.task_id).filter(
        Approval.status == "pending", Task.product_id == product_id).scalar() or 0


def _agent_ids(session) -> dict[str, uuid.UUID]:
    return {a.slug: a.id for a in session.query(Agent).filter(Agent.slug.in_(("prospecting", "sales", "growth"))).all()}


def _is_first_contact_draft(model=Task):
    return (model.context_refs["action"].astext == "draft_and_send") & (~model.context_refs.has_key("follow_up"))


def tick(company_id: uuid.UUID, product_id: uuid.UUID, *, now: Optional[datetime] = None, rng: Optional[random.Random] = None) -> dict:
    now, rng = now or _now(), rng or random.Random()
    actions: dict = {"research_tasks": 0, "draft_tasks": 0, "discovery_tasks": 0, "follow_up_runs": 0,
                     "expired": 0, "escalated": 0, "skipped": None}
    with session_scope() as session:
        state = outbound.ensure_state(session, product_id)
        state.last_tick_at = now
        enabled, paused = state.enabled, state.paused_reason
        cfg = load_cfg(state)
        cursor = dict(state.cursor or {})
        agents = _agent_ids(session)

    outbound.mark_stale_reservations(now)
    goal = campaign.evaluate(company_id, product_id, now)            # may complete/expire the campaign and raise its one incident
    actions["campaign"] = {"status": goal["status"], "pace": goal.get("pace")}
    if goal["status"] in ("completed", "expired"):
        actions["cancelled_on_campaign_end"] = _cancel_open_outreach(product_id, f"campaign {goal['status']}")
    tripped = outbound.evaluate_health(company_id, product_id, now)
    if tripped:
        paused = tripped
    actions["expired"] = _expire_stale_work(product_id, cfg, now)
    actions["escalated"] = _escalate_hot_leads(company_id, product_id, now)
    _approval_reminder(company_id, product_id, enabled, now)

    if not enabled:
        actions["skipped"] = "autopilot is OFF"
    elif goal["status"] in ("completed", "expired"):
        actions["skipped"] = f"campaign {goal['status']} -- no new outreach is planned"
    elif goal["status"] not in ("active",):
        actions["skipped"] = f"no active campaign goal ({goal['status']})"
    elif paused:
        actions["skipped"] = f"sending paused: {paused}"
    elif set(("prospecting", "sales", "growth")) - set(agents):
        actions["skipped"] = "agents not seeded"
    else:
        actions.update(_plan(company_id, product_id, cfg, cursor, agents, now, goal))

    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        state.cursor = {**(state.cursor or {}), **cursor}
    return actions


def _plan(company_id, product_id, cfg: AutopilotConfig, cursor: dict, agents: dict, now: datetime, goal: Optional[dict] = None) -> dict:
    out = {"research_tasks": 0, "draft_tasks": 0, "discovery_tasks": 0, "follow_up_runs": 0}
    pros_ctx = Context(company_id=company_id, agent_id=agents["prospecting"], product_id=product_id)
    sales_ctx = Context(company_id=company_id, agent_id=agents["sales"], product_id=product_id)
    growth_ctx = Context(company_id=company_id, agent_id=agents["growth"], product_id=product_id)
    redraft_cutoff = now - timedelta(days=REDRAFT_COOLDOWN_DAYS)

    with session_scope() as session:
        # ---- research for prospects that cleared the ingest gate
        researched_recently = {r[0] for r in session.query(Task.context_refs["prospect_id"].astext).filter(
            Task.product_id == product_id, Task.context_refs["action"].astext == "research", Task.created_at >= now - timedelta(days=7)).all()}
        to_research = [p.id for p in session.query(Prospect).filter_by(product_id=product_id, stage="enriched", do_not_contact=False)
                       .order_by(Prospect.icp_score.desc(), Prospect.created_at).limit(MAX_RESEARCH_PER_TICK * 2).all() if str(p.id) not in researched_recently][:MAX_RESEARCH_PER_TICK]
        enriched_count = session.query(func.count(Prospect.id)).filter_by(product_id=product_id, stage="enriched").scalar() or 0

        # ---- qualified prospects with no recent draft
        # A prospect is excluded if it has a draft that is STILL OPEN (whatever its age -- a draft may legitimately wait
        # days for approval) or had one very recently (a blocked/rejected/failed attempt must not loop).
        drafted_recently = {r[0] for r in session.query(Task.context_refs["prospect_id"].astext).filter(
            Task.product_id == product_id, _is_first_contact_draft(),
            (Task.status.in_(OPEN_TASK_STATUSES)) | (Task.created_at >= redraft_cutoff)).all()}
        dry_run_mode = get_settings().outreach_mode != "smtp"
        qualified = [p.id for p in session.query(Prospect).filter(
            Prospect.product_id == product_id, Prospect.stage == "qualified", Prospect.do_not_contact.is_(False),
            Prospect.qualification["passed"].astext == "true").order_by(Prospect.icp_score.desc(), Prospect.created_at).all()
            if str(p.id) not in drafted_recently
            # In dry run, a prospect already rehearsed is not re-drafted every cycle; it becomes eligible again the
            # moment outreach goes live (the rehearsal mark is ignored when OUTREACH_MODE=smtp).
            and not (dry_run_mode and (p.qualification or {}).get("dry_run"))]
        in_flight = session.query(func.count(Task.id)).filter(Task.product_id == product_id, Task.status.in_(OPEN_TASK_STATUSES), _is_first_contact_draft()).scalar() or 0
        open_discovery = session.query(func.count(Task.id)).filter(Task.product_id == product_id, Task.status.in_(OPEN_TASK_STATUSES),
                                                                    Task.context_refs["action"].astext == "discover").scalar() or 0
        open_followup_run = session.query(func.count(Task.id)).filter(Task.product_id == product_id, Task.status.in_(OPEN_TASK_STATUSES),
                                                                       Task.context_refs["action"].astext == "schedule_follow_ups").scalar() or 0
        last_discovery = session.query(Task).filter(Task.product_id == product_id, Task.created_by == "autopilot",
                                                    Task.context_refs["action"].astext == "discover", Task.status.in_(("done", "failed"))).order_by(Task.created_at.desc()).first()
        last_discovery_data = (str(last_discovery.id), last_discovery.status, dict(last_discovery.result or {}), last_discovery.last_error) if last_discovery else None
        first_send = outbound.first_send_at(session, company_id)
        used = outbound.count_today(session, company_id, cfg, now)

    cap = cfg.effective_cap(first_send, now)

    # ---- account for the last discovery's outcome (drives rotation / exhaustion)
    _absorb_discovery(company_id, product_id, cfg, cursor, last_discovery_data)

    for pid in to_research:
        TaskService.create(pros_ctx, goal="Research prospect and run the final qualification gate", created_by="autopilot",
                           context_refs={"action": "research", "prospect_id": str(pid), "origin": "autopilot"})
        out["research_tasks"] += 1

    # ---- drafts: bounded by capacity AND by how many prospects actually qualified. Never padded to hit a number.
    desired_in_flight = max(0, cap - used) + math.ceil(cap * cfg.buffer_days)
    new_drafts = max(0, min(cfg.max_new_drafts_per_tick, desired_in_flight - in_flight, len(qualified)))
    with session_scope() as session:
        names = {p.id: p.company_name for p in session.query(Prospect).filter(Prospect.id.in_(qualified[:new_drafts] or [uuid.uuid4()])).all()}
    for pid in qualified[:new_drafts]:
        TaskService.create(sales_ctx, goal=f"Draft and send first outreach to {names.get(pid, pid)}", created_by="autopilot", priority=3,
                           context_refs={"action": "draft_and_send", "prospect_id": str(pid), "origin": "autopilot", "company_name": names.get(pid)})
        out["draft_tasks"] += 1

    # ---- discovery: only when the pool of qualifiable prospects is running low
    stock = len(qualified) + enriched_count + in_flight          # everything on its way to being sendable
    trigger = math.ceil(cap * cfg.buffer_days)
    needed = (goal or {}).get("contacts_needed")
    if needed:
        # The goal drives the stock target once there is OBSERVED data: keep enough qualifiable prospects in the pipeline
        # to reach the contacts still needed (bounded by what the caps could send in the time left) -- not a fixed buffer.
        trigger = max(trigger, min(int(needed), campaign.capacity_ceiling(cfg, now, max(1, math.ceil(goal["days_left"])))))
    # Backoff: normal cooldown; 4x when every location is exhausted; doubling per consecutive FAILED discovery (capped at 48h)
    cooldown_h = cfg.discovery_cooldown_hours * (4 if cursor.get("exhausted") else 1) * (2 ** min(int(cursor.get("discovery_failures", 0)), 5))
    cooldown_h = max(cfg.discovery_cooldown_hours, min(cooldown_h, 48.0)) if cursor.get("discovery_failures") else cooldown_h
    last_at = _parse(cursor.get("last_discovery_at"))
    if stock < trigger and not open_discovery and (last_at is None or now - last_at >= timedelta(hours=cooldown_h)):
        geos = _geographies(product_id)
        geo = geos[cursor.get("geo_index", 0) % len(geos)] if geos else None
        refs = {"action": "discover", "limit": cfg.discovery_batch, "origin": "autopilot",
                "incident_key": "discovery_failing", "incident_severity": "warning"}
        if geo:
            refs["geography"] = geo
        TaskService.create(pros_ctx, goal=f"Discover new prospects{f' in {geo}' if geo else ''}", created_by="autopilot", priority=4, context_refs=refs)
        cursor["last_discovery_at"] = now.isoformat()
        out["discovery_tasks"] += 1

    # ---- the one follow-up
    last_fu = _parse(cursor.get("last_followup_run"))
    if not open_followup_run and (last_fu is None or now - last_fu >= timedelta(hours=FOLLOW_UP_CHECK_HOURS)):
        TaskService.create(growth_ctx, goal="Schedule follow-ups for silent prospects", created_by="autopilot", priority=2,
                           context_refs={"action": "schedule_follow_ups", "origin": "autopilot", "follow_up_after_days": cfg.follow_up_after_days})
        cursor["last_followup_run"] = now.isoformat()
        out["follow_up_runs"] += 1
    out["stock"], out["qualified_available"], out["capacity_remaining"] = stock, len(qualified), max(0, cap - used)
    return out


def _parse(value) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _geographies(product_id: uuid.UUID) -> list[str]:
    with session_scope() as session:
        config = session.get(ProductConfig, product_id)
        icp = (config.icp if config else None) or {}
    geos = list(icp.get("geographies") or [])
    if icp.get("geography") and icp["geography"] not in geos:
        geos.insert(0, icp["geography"])
    return geos


def _absorb_discovery(company_id, product_id, cfg, cursor: dict, last: Optional[tuple]) -> None:
    """Rotate geography when a place stops yielding anything new; say so out loud when every place is dry.
    A FAILED discovery (source down/blocked) is different from an EMPTY one: it backs the planner off exponentially and
    is reported by ONE de-duplicated incident (raised by the failing task itself), which resolves when discovery recovers."""
    if last is None or cursor.get("last_discovery_task_id") == last[0]:
        return
    task_id, status, result, error = last
    cursor["last_discovery_task_id"] = task_id
    if status == "failed":
        cursor["discovery_failures"] = int(cursor.get("discovery_failures", 0)) + 1
        cursor["last_discovery_error"] = (error or "")[:200]
        return
    cursor["discovery_failures"] = 0                         # any successful discovery = recovered: clear backoff, close the incident
    _resolve_incident(product_id, "discovery_failing")
    created = int(result.get("created", 0))
    cursor["total_discovered"] = int(cursor.get("total_discovered", 0)) + created
    if created > 0:
        cursor["empty_streak"], cursor["cycle_empty"], cursor["exhausted"] = 0, 0, False
        return
    cursor["empty_streak"] = int(cursor.get("empty_streak", 0)) + 1
    geos = _geographies(product_id)
    if cursor["empty_streak"] >= 2:
        cursor["geo_index"], cursor["empty_streak"] = int(cursor.get("geo_index", 0)) + 1, 0
        cursor["cycle_empty"] = int(cursor.get("cycle_empty", 0)) + 1
    if geos and cursor.get("cycle_empty", 0) >= len(geos):
        cursor["exhausted"] = True
        IncidentService().report(
            company_id=company_id, product_id=product_id, agent_id=None, task_id=None, severity="warning",
            title="Prospect discovery has run dry",
            cause=f"The last several discovery runs found nothing new in any configured location ({', '.join(geos) or 'none'}).",
            impact="The pipeline will empty out. The system will NOT lower its qualification standards to compensate.",
            action_required="Add more icp.geographies, broaden icp.industries/osm_tags, or import prospects from your own list.",
            dedup_key="discovery_exhausted")


def _cancel_open_outreach(product_id: uuid.UUID, reason: str) -> int:
    """When a campaign ends, nothing unsent may fire later: cancel open (pending/awaiting-approval) autopilot outreach."""
    n = 0
    with session_scope() as session:
        for task in session.query(Task).filter(Task.product_id == product_id, Task.status.in_(("needs_approval", "pending")),
                                               Task.context_refs["action"].astext == "draft_and_send", Task.context_refs["origin"].astext == "autopilot").all():
            task.status, task.last_error = "cancelled", reason
            session.query(Approval).filter_by(task_id=task.id, status="pending").update({"status": "expired", "decided_at": _now()})
            n += 1
    return n


def _resolve_incident(product_id: uuid.UUID, dedup_key: str) -> None:
    with session_scope() as session:
        for incident in session.query(Incident).filter_by(product_id=product_id, dedup_key=dedup_key, status="open").all():
            incident.status, incident.resolved_at = "resolved", _now()


def _expire_stale_work(product_id: uuid.UUID, cfg: AutopilotConfig, now: datetime) -> int:
    """A draft nobody approved for N days, or an approved send that never got to go (paused, window never opened),
    must not suddenly fire weeks later on stale information. Cancel it; the prospect can be re-drafted fresh."""
    cutoff = now - timedelta(days=cfg.max_task_age_days)
    n = 0
    with session_scope() as session:
        stale = session.query(Task).filter(Task.product_id == product_id, Task.status.in_(("needs_approval", "pending")),
                                           Task.context_refs["action"].astext == "draft_and_send", Task.created_at < cutoff).all()
        for task in stale:
            if task.created_by not in ("autopilot", "growth"):
                continue                                           # never auto-cancel something the founder created by hand
            task.status, task.last_error = "cancelled", f"expired after {cfg.max_task_age_days} days without being sent"
            session.query(Approval).filter_by(task_id=task.id, status="pending").update({"status": "expired", "decided_at": now})
            n += 1
    return n


def _escalate_hot_leads(company_id: uuid.UUID, product_id: uuid.UUID, now: datetime) -> int:
    sla = timedelta(hours=get_settings().hot_lead_sla_hours)
    with session_scope() as session:
        stale = [i.id for i in session.query(Incident).filter(
            Incident.product_id == product_id, Incident.status == "open", Incident.dedup_key.like("hot_lead:%"),
            Incident.escalated_at.is_(None), Incident.created_at < now - sla).all()]
    service = IncidentService()
    for incident_id in stale:
        service.escalate(incident_id, f"No response logged after {get_settings().hot_lead_sla_hours:g} hours.")
    return len(stale)


def _approval_reminder(company_id: uuid.UUID, product_id: uuid.UUID, enabled: bool, now: datetime) -> None:
    hours = get_settings().approval_reminder_hours
    with session_scope() as session:
        rows = session.query(Approval.created_at).join(Task, Task.id == Approval.task_id).filter(
            Approval.status == "pending", Approval.requested_by == "sales", Task.product_id == product_id).all()
        waiting = [r[0] for r in rows if (r[0] if r[0].tzinfo else r[0].replace(tzinfo=timezone.utc)) < now - timedelta(hours=hours)]
        open_incident = session.query(Incident).filter_by(product_id=product_id, dedup_key="approvals_waiting", status="open").first()
        if not waiting and open_incident is not None:
            open_incident.status, open_incident.resolved_at = "resolved", now          # cleared by the founder: close it
            return
    if waiting and enabled:
        oldest = min(w if w.tzinfo else w.replace(tzinfo=timezone.utc) for w in waiting)
        IncidentService().report(
            company_id=company_id, product_id=product_id, agent_id=None, task_id=None, severity="warning",
            title=f"{len(waiting)} outreach draft(s) waiting for your approval",
            cause=f"The oldest has been waiting {(now - oldest).total_seconds() / 3600:.0f}h.",
            impact="Approved emails are what move prospects toward a customer; unapproved drafts expire.",
            action_required="Review and approve: python -m scripts.autopilot_ctl approvals list  (then approve --all, or approve ID)",
            dedup_key="approvals_waiting")
