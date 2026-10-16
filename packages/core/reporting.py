"""
Growth reporting: what happened, how it converts, what is broken, and what needs the founder -- computed from real
rows only, stored (growth_reports) and delivered (email / Slack / console). Daily and weekly; idempotent per period.

Honest about small numbers: rates are reported with their sample sizes, and a bottleneck is only named when there is
enough data to justify it -- the same discipline as the funnel baseline.
"""
from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func

from packages.core import campaign, funnel, outbound
from packages.core.autopilot_config import AutopilotConfig
from packages.core.config import get_settings
from packages.core.log import get_logger, log
from packages.db.models import (
    Approval, AutopilotState, FunnelEvent, GrowthReport, Incident, OutboundMessage, Product, Prospect, Task,
)
from packages.db.session import session_scope

_log = get_logger("reporting")
POSITIVE = ("interested", "high_intent")
NEGATIVE = ("not_interested", "unsubscribe", "spam", "wrong_person", "existing_customer")
MIN_RATE_SAMPLE = 20


def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 3) if d else None


def _pct(r: Optional[float]) -> str:
    return "n/a" if r is None else f"{r:.0%}"


def compute_metrics(company_id: uuid.UUID, product_id: uuid.UUID, start: datetime, end: datetime, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        def stage_reached(stage: str, lo=start, hi=end) -> int:
            return session.query(func.count(func.distinct(FunnelEvent.prospect_id))).filter(
                FunnelEvent.product_id == product_id, FunnelEvent.to_stage == stage, FunnelEvent.created_at >= lo, FunnelEvent.created_at < hi).scalar() or 0

        found = session.query(func.count(Prospect.id)).filter(Prospect.product_id == product_id, Prospect.created_at >= start, Prospect.created_at < end).scalar() or 0
        ingest_failed = session.query(func.count(Prospect.id)).filter(
            Prospect.product_id == product_id, Prospect.created_at >= start, Prospect.created_at < end,
            Prospect.stage == "icp_match", Prospect.qualification["stage"].astext == "ingest").scalar() or 0
        qualified = stage_reached("qualified")
        disqualified_after_research = session.query(func.count(FunnelEvent.id)).filter(
            FunnelEvent.product_id == product_id, FunnelEvent.from_stage == "enriched", FunnelEvent.to_stage == "icp_match",
            FunnelEvent.created_at >= start, FunnelEvent.created_at < end).scalar() or 0

        reasons: Counter = Counter()
        for (q,) in session.query(Prospect.qualification).filter(
                Prospect.product_id == product_id, Prospect.updated_at >= start, Prospect.updated_at < end, Prospect.qualification.isnot(None),
                Prospect.qualification["passed"].astext == "false").limit(1000).all():
            for failed in (q or {}).get("failed", []):
                reasons[failed] += 1

        out = session.query(OutboundMessage).filter(OutboundMessage.company_id == company_id, OutboundMessage.product_id == product_id,
                                                    OutboundMessage.reserved_at >= start, OutboundMessage.reserved_at < end).all()
        real = ("sent", "unknown", "bounced")
        first = [m for m in out if m.kind == "first_contact" and m.status in real]
        follow = [m for m in out if m.kind == "follow_up" and m.status in real]
        dry = [m for m in out if m.status == "logged"]
        bounces_hard = session.query(func.count(OutboundMessage.id)).filter(OutboundMessage.company_id == company_id, OutboundMessage.product_id == product_id,
                                                                            OutboundMessage.bounced_at >= start, OutboundMessage.bounced_at < end, OutboundMessage.bounce_type == "hard").scalar() or 0
        send_failures = len([m for m in out if m.status == "failed"])

        replies = Counter(r[0] for r in session.query(Prospect.reply_classification).filter(
            Prospect.product_id == product_id, Prospect.replied_at >= start, Prospect.replied_at < end).all())
        n_replies = sum(replies.values())
        positive = sum(replies[c] for c in POSITIVE)
        meetings, customers = stage_reached("meeting_booked"), stage_reached("customer")

        failed_tasks = session.query(func.count(Task.id)).filter(Task.product_id == product_id, Task.status == "failed",
                                                                  Task.updated_at >= start, Task.updated_at < end).scalar() or 0
        incidents = dict(session.query(Incident.severity, func.count()).filter(Incident.product_id == product_id, Incident.created_at >= start, Incident.created_at < end).group_by(Incident.severity).all())
        auto_sends = len([m for m in first + follow if m.approval == "auto"])

        total = lambda stage: session.query(func.count(func.distinct(FunnelEvent.prospect_id))).filter(FunnelEvent.product_id == product_id, FunnelEvent.to_stage == stage).scalar() or 0   # noqa: E731
        cumulative = {
            "prospects": session.query(func.count(Prospect.id)).filter_by(product_id=product_id).scalar() or 0,
            "qualified": total("qualified"),
            "contacted": session.query(func.count(func.distinct(OutboundMessage.prospect_id))).filter(
                OutboundMessage.company_id == company_id, OutboundMessage.product_id == product_id, OutboundMessage.kind == "first_contact", OutboundMessage.status.in_(real)).scalar() or 0,
            "positive_replies": session.query(func.count(Prospect.id)).filter(Prospect.product_id == product_id, Prospect.reply_classification.in_(POSITIVE)).scalar() or 0,
            "meetings": total("meeting_booked"), "customers": total("customer"),
        }
        state = session.get(AutopilotState, product_id)
        cfg = AutopilotConfig.from_dict(state.config if state else {})
        goal = campaign.progress(product_id, now)
        needs = [{"title": i.title, "severity": i.severity, "since": i.created_at.isoformat(), "action": i.action_required}
                 for i in session.query(Incident).filter(Incident.product_id == product_id, Incident.status == "open",
                                                          Incident.severity.in_(("warning", "error", "critical")) | Incident.dedup_key.like("hot_lead:%") | Incident.dedup_key.like("reply_needs_human:%"))
                 .order_by(Incident.created_at).limit(15).all()]
        pending_approvals = session.query(func.count(Approval.id)).join(Task, Task.id == Approval.task_id).filter(Approval.status == "pending", Task.product_id == product_id).scalar() or 0
        paused = state.paused_reason if state else None
        events = funnel.load_events(session, product_id)

    contacted_n = len(first)
    metrics = {
        "period": {"start": start.isoformat(), "end": end.isoformat()}, "partial": end >= now - timedelta(seconds=1),
        "prospects_found": found, "failed_ingest_gate": ingest_failed, "qualified": qualified, "disqualified_after_research": disqualified_after_research,
        "qualification_pass_rate": _rate(qualified, qualified + disqualified_after_research + ingest_failed),
        "top_disqualification_reasons": reasons.most_common(6),
        "first_contacts_sent": contacted_n, "follow_ups_sent": len(follow), "dry_run_logged": len(dry), "auto_sent": auto_sends,
        "replies": n_replies, "reply_breakdown": dict(replies), "positive_replies": positive,
        "reply_rate": _rate(n_replies, contacted_n), "positive_reply_rate": _rate(positive, contacted_n),
        "meetings_booked": meetings, "customers": customers,
        "hard_bounces": bounces_hard, "bounce_rate": _rate(bounces_hard, contacted_n + len(follow)),
        "unsubscribes": replies.get("unsubscribe", 0), "send_failures": send_failures, "failed_tasks": failed_tasks, "incidents_opened": incidents,
        "cumulative": cumulative, "goal": goal, "target_customers": goal.get("target"), "customers_remaining": goal.get("remaining"),
        "pending_approvals": pending_approvals, "paused": paused, "needs_founder": needs,
        "funnel_baseline": funnel.compute_funnel_report(events, now=now),
    }
    return metrics


def find_bottlenecks(m: dict, cfg: Optional[AutopilotConfig] = None) -> list[dict]:
    b: list[dict] = []
    add = lambda area, sev, finding, suggestion: b.append({"area": area, "severity": sev, "finding": finding, "suggestion": suggestion})   # noqa: E731
    cfg = cfg or AutopilotConfig()
    if m["paused"]:
        add("deliverability", "critical", f"Sending is PAUSED: {m['paused']}", "Fix the cause, then resume (autopilot_ctl resume --ack).")
    if m["pending_approvals"]:
        add("approvals", "warning" if m["pending_approvals"] >= 5 else "info", f"{m['pending_approvals']} draft(s) are waiting for your approval.",
            "Approve them (autopilot_ctl approvals approve --all) -- unapproved drafts expire and capacity goes unused.")
    evaluated = m["qualified"] + m["disqualified_after_research"] + m["failed_ingest_gate"]
    if evaluated >= MIN_RATE_SAMPLE and (m["qualification_pass_rate"] or 0) < 0.15:
        top = m["top_disqualification_reasons"][0] if m["top_disqualification_reasons"] else None
        add("qualification", "warning", f"Only {_pct(m['qualification_pass_rate'])} of {evaluated} prospects pass the gate" + (f" (most common failure: {top[0]}, {top[1]}x)" if top else "") + ".",
            "Tighten the discovery source/ICP so fewer unqualifiable businesses are fetched -- do not loosen the gate.")
    if m["prospects_found"] == 0 and m["first_contacts_sent"] == 0 and not m.get("partial"):      # a half-finished day proves nothing
        add("discovery", "warning", "No new prospects were found this period.", "Check ENRICHMENT_MODE=free, the ICP geography/industries, and `autopilot_ctl status` (discovery may be exhausted).")
    if m["first_contacts_sent"] >= MIN_RATE_SAMPLE and (m["reply_rate"] or 0) < 0.01:
        add("messaging", "warning", f"{m['first_contacts_sent']} emails sent but reply rate is {_pct(m['reply_rate'])}.",
            "Review the copy/positioning and ICP fit; check deliverability (SPF/DKIM/DMARC, inbox placement).")
    if (m["bounce_rate"] or 0) >= 0.03 and m["first_contacts_sent"] >= 10:
        add("list_quality", "warning", f"Hard-bounce rate is {_pct(m['bounce_rate'])}.", "Addresses scraped from sites are going stale; tighten email checks or prefer named mailboxes.")
    if m["positive_replies"] and not m["meetings_booked"]:
        add("conversion", "warning", f"{m['positive_replies']} positive repl(ies) but no meeting booked yet.", "Answer the hot leads quickly -- they are listed under 'needs you'.")
    if m["failed_tasks"] or m["send_failures"]:
        add("reliability", "warning", f"{m['failed_tasks']} task(s) and {m['send_failures']} send(s) failed.", "See the Incidents list for causes; retries already happened automatically.")
    goal = m.get("goal") or {}
    if goal.get("pace") in ("behind", "at_risk"):
        add("goal", "warning", goal["message"], "Look at the other bottlenecks first (targeting, copy, replies awaiting you), or extend the deadline.")
    if goal.get("status") == "expired":
        add("goal", "warning", goal["message"], "Extend the deadline or set a new goal.")
    for t in (m["funnel_baseline"].get("degraded") or []):
        add("funnel", "warning", f"Conversion at {t} has fallen well below its own baseline.", "Diagnose that step before sending more volume.")
    leak = m["funnel_baseline"].get("largest_leak")
    if leak and not m["funnel_baseline"].get("degraded"):
        add("funnel", "info", f"Largest leak: {leak['transition']} ({leak['prospects_not_advanced']} prospects not advanced).", "This is where improvement pays most.")
    return b


def render_text(product_name: str, period: str, m: dict, bottlenecks: list[dict]) -> str:
    c = m["cumulative"]
    lines = [f"{product_name} acquisition -- {period} report ({m['period']['start'][:10]} to {m['period']['end'][:10]})" + ("  [PARTIAL: period still in progress]" if m.get("partial") else ""), ""]
    lines.append("GOAL: " + m["goal"]["message"])
    if m["goal"].get("customers_from_outreach") is not None and m["goal"].get("customers"):
        lines.append(f"      ({m['goal']['customers_from_outreach']} of those came through the outreach loop)")
    if m["paused"]:
        lines += ["", f"!! SENDING PAUSED: {m['paused']}"]
    lines += ["", "THIS PERIOD",
              f"  Prospects found: {m['prospects_found']}   Qualified: {m['qualified']}   (pass rate {_pct(m['qualification_pass_rate'])}; "
              f"{m['failed_ingest_gate']} failed ingest, {m['disqualified_after_research']} failed research)",
              f"  Emails sent: {m['first_contacts_sent']} first contact, {m['follow_ups_sent']} follow-up" + (f"   [DRY RUN: {m['dry_run_logged']} logged, not sent]" if m["dry_run_logged"] else ""),
              f"  Replies: {m['replies']} (reply rate {_pct(m['reply_rate'])}), positive: {m['positive_replies']} ({_pct(m['positive_reply_rate'])})",
              f"  Meetings booked: {m['meetings_booked']}   New customers: {m['customers']}",
              f"  Hard bounces: {m['hard_bounces']}   Unsubscribes: {m['unsubscribes']}   Failures: {m['failed_tasks']} tasks, {m['send_failures']} sends"]
    if m["top_disqualification_reasons"]:
        lines.append("  Why prospects were not contacted: " + ", ".join(f"{k} x{v}" for k, v in m["top_disqualification_reasons"][:4]))
    lines += ["", f"ALL TIME: {c['prospects']} prospects, {c['qualified']} qualified, {c['contacted']} contacted, {c['positive_replies']} positive replies, {c['meetings']} meetings, {c['customers']} customers"]
    if bottlenecks:
        lines += ["", "BOTTLENECKS"] + [f"  [{x['severity'].upper()}] {x['area']}: {x['finding']} -> {x['suggestion']}" for x in bottlenecks]
    if m["needs_founder"] or m["pending_approvals"]:
        lines += ["", "NEEDS YOU"]
        if m["pending_approvals"]:
            lines.append(f"  - {m['pending_approvals']} outreach draft(s) awaiting approval")
        lines += [f"  - {n['title']}" for n in m["needs_founder"][:10]]
    else:
        lines += ["", "NEEDS YOU: nothing -- the system is running on its own."]
    return "\n".join(lines)


def generate(company_id: uuid.UUID, product_id: uuid.UUID, period: str, start: datetime, end: datetime, now: Optional[datetime] = None) -> Optional[uuid.UUID]:
    """Idempotent: returns None if this period's report already exists."""
    with session_scope() as session:
        if session.query(GrowthReport.id).filter_by(product_id=product_id, period=period, period_start=start).first():
            return None
        product = session.get(Product, product_id)
        name = product.name if product else "Product"
        state = session.get(AutopilotState, product_id)
        cfg = AutopilotConfig.from_dict(state.config if state else {})
    m = compute_metrics(company_id, product_id, start, end, now)
    b = find_bottlenecks(m, cfg)
    text = render_text(name, period, m, b)
    with session_scope() as session:
        report = GrowthReport(id=uuid.uuid4(), company_id=company_id, product_id=product_id, period=period, period_start=start, period_end=end,
                              metrics=m, bottlenecks=b, summary=text)
        session.add(report)
        session.flush()
        return report.id


def deliver(report_id: uuid.UUID, *, smtp_factory=None, http_post=None) -> list[str]:
    from packages.core import mailer
    settings = get_settings()
    with session_scope() as session:
        report = session.get(GrowthReport, report_id)
        text, period = report.summary, report.period
        product = session.get(Product, report.product_id)
        subject = f"{product.name if product else 'Company'} {period} growth report"
    channels = []
    if mailer.can_send_internal(settings):
        try:
            mailer.send_internal_email(settings, subject, text, smtp_factory)
            channels.append("email")
        except Exception as exc:
            log(_log, logging.ERROR, "report email failed", error=exc.__class__.__name__)
    if settings.has_live_notifications:
        try:
            import httpx
            (http_post or httpx.post)(settings.slack_webhook_url, json={"text": text}, timeout=8.0)
            channels.append("slack")
        except Exception as exc:
            log(_log, logging.ERROR, "report slack post failed", error=exc.__class__.__name__)
    if not channels:
        print(text)  # noqa: T201 -- the $0 fallback: at least the console shows it
        channels.append("console")
    with session_scope() as session:
        session.get(GrowthReport, report_id).delivered_at = datetime.now(timezone.utc)
    return channels


def maybe_generate(company_id: uuid.UUID, product_id: uuid.UUID, now: Optional[datetime] = None, *, deliver_now: bool = True) -> list[uuid.UUID]:
    """Daily at report_hour (local), covering the previous local day; weekly on report weekday, covering the previous 7 days."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        if state is None:
            return []
        cfg = AutopilotConfig.from_dict(state.config)
        has_activity = state.enabled or bool(session.query(OutboundMessage.id).filter_by(company_id=company_id, product_id=product_id).first())
    if not has_activity:
        return []
    local = cfg.local(now)
    if local.hour < cfg.report_hour:
        return []
    midnight = cfg.day_start_utc(now)
    created: list[uuid.UUID] = []
    daily = generate(company_id, product_id, "daily", midnight - timedelta(days=1), midnight, now)
    if daily:
        created.append(daily)
    if local.weekday() == cfg.weekly_report_weekday:
        weekly = generate(company_id, product_id, "weekly", midnight - timedelta(days=7), midnight, now)
        if weekly:
            created.append(weekly)
    if deliver_now:
        for rid in created:
            deliver(rid)
    return created
