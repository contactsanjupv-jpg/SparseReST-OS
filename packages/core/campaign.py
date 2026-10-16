"""
THE FOUNDER'S CAMPAIGN GOAL: "N customers in D days".

Supplied by the founder (no default -- autopilot refuses to start without one), stored per product, and USED:

  lifecycle   unset -> pending (goal set) -> active (clock starts when autopilot is enabled) -> completed | expired.
              Customers are counted from the moment the campaign starts, so "5 in 30 days" means 5 NEW customers in those 30 days.
  control     once completed or expired the planner plans nothing new, unsent drafts are cancelled, and the outbound
              governor refuses autopilot sends (a backstop). Replies, hot-lead alerts and reports keep running.
  pace        an honest assessment from OBSERVED data only: no invented conversion rates. Too early -> insufficient data;
              later a straight-line projection from customers won so far; plus the arithmetic ceiling (the most first
              emails the caps allow in the window) and the end-to-end close rate that would be needed to hit the target.
  planning    once there is observed data (>= MIN_CONTACTS first emails and >= 1 customer) the planner sizes its
              prospect stock to the contacts still needed to reach the target, instead of a fixed buffer.
  escalation  falling behind pace raises ONE de-duplicated warning; goal reached / deadline passed raise one each.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func

from packages.core.autopilot_config import AutopilotConfig
from packages.core.log import get_logger, log
from packages.db.models import AutopilotState, FunnelEvent, Incident, OutboundMessage, Product
from packages.db.session import session_scope

_log = get_logger("campaign")
MAX_CUSTOMERS, MAX_DAYS = 1000, 365
MIN_CONTACTS_FOR_RATE = 20
MIN_DAYS_FOR_PROJECTION = 7
REAL = ("sent", "unknown", "bounced")


class GoalError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def validate_goal(customers, days) -> tuple[int, int]:
    try:
        customers, days = int(customers), int(days)
    except (TypeError, ValueError):
        raise GoalError("customers and days must be whole numbers")
    if not 1 <= customers <= MAX_CUSTOMERS:
        raise GoalError(f"customers must be between 1 and {MAX_CUSTOMERS}")
    if not 1 <= days <= MAX_DAYS:
        raise GoalError(f"days must be between 1 and {MAX_DAYS}")
    return customers, days


# ---------------------------------------------------------------- founder actions
def set_goal(product_id: uuid.UUID, customers, days, *, by: str = "founder", now: Optional[datetime] = None) -> dict:
    """Define (or replace) the campaign. The clock starts when autopilot is enabled -- or immediately if it already is."""
    customers, days = validate_goal(customers, days)
    now = now or _now()
    with session_scope() as session:
        from packages.core import outbound
        state = outbound.ensure_state(session, product_id)
        state.campaign_target, state.campaign_days = customers, days
        state.campaign_started_at = state.campaign_ended_at = None
        state.campaign_status, state.updated_by = "pending", by
        if state.enabled:
            state.campaign_status, state.campaign_started_at = "active", now
    _resolve(product_id, ("campaign_complete", "campaign_expired", "campaign_behind"))
    log(_log, logging.WARNING, "campaign goal set", customers=customers, days=days, by=by, product_id=str(product_id))
    return progress(product_id, now)


def extend(product_id: uuid.UUID, days: int, *, now: Optional[datetime] = None) -> dict:
    """Add days to the deadline WITHOUT restarting the count. Re-activates an expired campaign."""
    now = now or _now()
    if not 1 <= int(days) <= MAX_DAYS:
        raise GoalError(f"days must be between 1 and {MAX_DAYS}")
    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        if state is None or state.campaign_status in ("unset", "pending"):
            raise GoalError("there is no running campaign to extend -- set a goal first")
        if state.campaign_status == "completed":
            raise GoalError("this campaign already reached its goal -- set a new goal instead")
        state.campaign_days = min(MAX_DAYS, int(state.campaign_days) + int(days))
        if state.campaign_status == "expired" and (_aware(state.campaign_started_at) + timedelta(days=state.campaign_days)) > now:
            state.campaign_status, state.campaign_ended_at = "active", None
    _resolve(product_id, ("campaign_expired",))
    return progress(product_id, now)


def activate_if_pending(session, state: AutopilotState, now: Optional[datetime] = None) -> None:
    """Called when autopilot is enabled: this is when 'N customers in D days' starts counting."""
    if state.campaign_status == "pending":
        state.campaign_status, state.campaign_started_at = "active", now or _now()


# ---------------------------------------------------------------- measurement
def customers_since(session, product_id: uuid.UUID, since: datetime) -> tuple[int, int]:
    """(all customers recorded since the campaign began, of which came through the outreach loop)."""
    from packages.db.models import Prospect
    ids = [r[0] for r in session.query(FunnelEvent.prospect_id).filter(
        FunnelEvent.product_id == product_id, FunnelEvent.to_stage == "customer", FunnelEvent.created_at >= since).distinct().all()]
    if not ids:
        return 0, 0
    emails = [r[0].strip().lower() for r in session.query(Prospect.contact_email).filter(Prospect.id.in_(ids), Prospect.contact_email.isnot(None)).all()]
    from_outreach = session.query(func.count(func.distinct(OutboundMessage.to_email))).filter(
        OutboundMessage.to_email.in_(emails or ["-"]), OutboundMessage.kind == "first_contact", OutboundMessage.status.in_(REAL)).scalar() or 0
    return len(ids), int(from_outreach)


def first_contacts_since(session, company_id: uuid.UUID, since: datetime) -> int:
    return session.query(func.count(OutboundMessage.id)).filter(
        OutboundMessage.company_id == company_id, OutboundMessage.kind == "first_contact", OutboundMessage.status.in_(REAL), OutboundMessage.reserved_at >= since).scalar() or 0


def capacity_ceiling(cfg: AutopilotConfig, start: datetime, days: int) -> int:
    """The MOST first emails the configured caps (with warm-up) could send in the campaign window. A ceiling, not a forecast."""
    total = 0
    for d in range(days):
        day = start + timedelta(days=d)
        if cfg.local(day).weekday() in cfg.send_days:
            total += cfg.effective_cap(start, day)
    return total


def progress(product_id: uuid.UUID, now: Optional[datetime] = None) -> dict:
    """Everything about the goal, computed from real rows. Pure read -- never changes state (see `evaluate`)."""
    now = now or _now()
    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        if state is None or state.campaign_status == "unset" or not state.campaign_target:
            return {"status": "unset", "target": None, "days": None, "message": "No goal set. Set one: autopilot_ctl goal set --customers N --days D"}
        cfg = AutopilotConfig.from_dict(state.config or {})
        status, target, days = state.campaign_status, int(state.campaign_target), int(state.campaign_days)
        started, ended = _aware(state.campaign_started_at), _aware(state.campaign_ended_at)
        company_id = session.get(Product, product_id).company_id
        if started is None:
            return {"status": "pending", "target": target, "days": days, "customers": 0, "remaining": target,
                    "message": f"Goal: {target} customers in {days} days. The clock starts when you enable autopilot."}
        deadline = started + timedelta(days=days)
        customers, from_outreach = customers_since(session, product_id, started)
        contacts = first_contacts_since(session, company_id, started)

    elapsed = max(0.0, ((ended or now) - started).total_seconds() / 86400)
    left = max(0.0, (deadline - now).total_seconds() / 86400)
    remaining = max(0, target - customers)
    ceiling = capacity_ceiling(cfg, started, days)
    out = {
        "status": status, "target": target, "days": days, "customers": customers, "customers_from_outreach": from_outreach,
        "remaining": remaining, "started_at": started.isoformat(), "deadline": deadline.isoformat(),
        "days_elapsed": round(elapsed, 1), "days_left": round(left, 1), "first_contacts": contacts,
        "capacity_ceiling": ceiling, "required_close_rate_at_full_capacity": round(target / ceiling, 4) if ceiling else None,
        "required_per_week": round(remaining / max(left / 7, 1 / 7), 2) if status == "active" else None,
        "contacts_needed": None, "projected_customers": None, "pace": "n/a",
    }
    if status == "completed":
        out["pace"] = "completed"
    elif status == "expired":
        out["pace"] = "expired"
    else:
        if customers >= 1 and contacts >= MIN_CONTACTS_FOR_RATE:
            rate = customers / contacts
            out["observed_close_rate"] = round(rate, 4)
            out["contacts_needed"] = math.ceil(remaining / rate) if remaining else 0
        if elapsed >= MIN_DAYS_FOR_PROJECTION:
            # Never extrapolate from the first few days: one early customer on day 0 would "project" to the moon.
            projected = customers / elapsed * days
            out["projected_customers"] = round(projected, 1)
            out["pace"] = "on_track" if projected >= target else ("behind" if projected >= 0.5 * target else "at_risk")
        else:
            out["pace"] = "too_early"
    out["message"] = describe(out)
    return out


def describe(p: dict) -> str:
    if p["status"] in ("unset", "pending"):
        return p.get("message", "")
    head = f"{p['customers']} / {p['target']} customers in {p['days']} days (day {p['days_elapsed']:.0f}, {p['days_left']:.0f} left)"
    tail = {"completed": "GOAL REACHED", "expired": "DEADLINE PASSED -- extend or set a new goal", "too_early": "too early to judge pace",
            "on_track": f"on track (projected ~{p['projected_customers']})", "behind": f"BEHIND pace (projected ~{p['projected_customers']})",
            "at_risk": f"AT RISK (projected ~{p['projected_customers']})"}.get(p["pace"], "")
    extra = ""
    rc = p.get("required_close_rate_at_full_capacity")
    if p["status"] == "active" and rc:
        extra = f" | needs ~{rc:.1%} of the at-most {p['capacity_ceiling']} emails the caps allow to become customers"
        if rc >= 0.01:
            extra += " (cold outreach usually converts far fewer -- consider more days)"
    return f"{head}: {tail}{extra}"


# ---------------------------------------------------------------- lifecycle (called by the planner)
def evaluate(company_id: uuid.UUID, product_id: uuid.UUID, now: Optional[datetime] = None) -> dict:
    """Advance the campaign state machine (completed / expired), raise the one-per-condition incidents, return progress."""
    from packages.core.incidents import IncidentService
    now = now or _now()
    p = progress(product_id, now)
    if p["status"] != "active":
        return p
    transition = None
    if p["customers"] >= p["target"]:
        transition = "completed"
    elif now >= datetime.fromisoformat(p["deadline"]):
        transition = "expired"
    if transition:
        with session_scope() as session:
            state = session.get(AutopilotState, product_id)
            state.campaign_status, state.campaign_ended_at = transition, now
        p = progress(product_id, now)
        service = IncidentService()
        if transition == "completed":
            service.report(company_id=company_id, product_id=product_id, agent_id=None, task_id=None, severity="info", dedup_key="campaign_complete",
                           title=f"GOAL REACHED: {p['customers']} / {p['target']} customers",
                           cause="The campaign target was met.", impact="New outreach has stopped; replies and reports continue.",
                           action_required="Set a new goal to continue: autopilot_ctl goal set --customers N --days D")
        else:
            service.report(company_id=company_id, product_id=product_id, agent_id=None, task_id=None, severity="warning", dedup_key="campaign_expired",
                           title=f"Campaign deadline reached: {p['customers']} / {p['target']} customers",
                           cause=f"{p['days']} days have passed.", impact="New outreach has stopped; replies and reports continue.",
                           action_required="Extend (autopilot_ctl goal extend --days N) or set a new goal.")
        _resolve(product_id, ("campaign_behind",))
        return p
    if p["pace"] in ("behind", "at_risk"):
        IncidentService().report(
            company_id=company_id, product_id=product_id, agent_id=None, task_id=None, severity="warning", dedup_key="campaign_behind",
            title=f"Campaign {p['pace'].replace('_', ' ')}: {p['customers']} / {p['target']} customers, {p['days_left']:.0f} days left",
            cause=p["message"], impact="At the current pace the target will be missed.",
            action_required="Read the weekly report's bottlenecks (targeting, copy, replies awaiting you) -- or extend the deadline.")
    elif p["pace"] in ("on_track", "too_early"):
        _resolve(product_id, ("campaign_behind",))
    return p


def _resolve(product_id: uuid.UUID, keys: tuple[str, ...]) -> None:
    with session_scope() as session:
        for incident in session.query(Incident).filter(Incident.product_id == product_id, Incident.dedup_key.in_(keys), Incident.status == "open").all():
            incident.status, incident.resolved_at = "resolved", _now()


def is_active(product_id: uuid.UUID) -> bool:
    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        return bool(state and state.campaign_status == "active")
