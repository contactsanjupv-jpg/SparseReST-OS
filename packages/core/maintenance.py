"""
Periodic housekeeping, run by exactly one process at a time (guarded by an advisory lock in
scripts/run_worker.py, and runnable on demand from the control plane).

Each step is isolated: one failing step is logged and reported, never allowed to stop the others.
"""
from __future__ import annotations

import logging
from typing import Callable

from packages.core.log import get_logger, log

_log = get_logger("maintenance")
ACTIVE_LIFECYCLES = ("beta", "active")


def _step_requeue_stuck() -> int:
    from packages.core.queue import requeue_stuck_tasks
    return len(requeue_stuck_tasks())


def _step_purge_sessions() -> int:
    from apps.api.auth_service import AuthService
    return AuthService.purge_expired_sessions()


def _step_prune_heartbeats() -> int:
    from packages.core.workers import HeartbeatService
    return HeartbeatService.prune()


def _step_funnel_checks() -> int:
    """For every live product: compare the recent funnel with its own baseline and raise (one,
    de-duplicated) warning Incident per degraded transition."""
    from packages.core.funnel import funnel_report_for_product, report_degradation_incidents
    from packages.db.models import Agent, Product
    from packages.db.session import session_scope

    with session_scope() as session:
        growth_id = session.query(Agent).filter_by(slug="growth").one().id
        products = [(p.company_id, p.id) for p in session.query(Product).filter(Product.lifecycle.in_(ACTIVE_LIFECYCLES)).all()]

    raised = 0
    for company_id, product_id in products:
        report = funnel_report_for_product(product_id)
        raised += len(report_degradation_incidents(company_id, product_id, report, growth_agent_id=growth_id))
    return raised


def _step_autopilot() -> int:
    """One planning pass + report check for every product that has an autopilot record. Each product is
    isolated: one product's failure never stops another's tick. Returns the number of tasks created."""
    from packages.core import autopilot, reporting
    from packages.db.models import AutopilotState, Product
    from packages.db.session import session_scope

    with session_scope() as session:
        targets = [(p.company_id, p.id) for p in session.query(Product).join(AutopilotState, AutopilotState.product_id == Product.id).all()]
    created = 0
    for company_id, product_id in targets:
        try:
            result = autopilot.tick(company_id, product_id)
            created += sum(v for k, v in result.items() if k.endswith("_tasks") or k == "follow_up_runs")
            reporting.maybe_generate(company_id, product_id)
        except Exception as exc:
            log(_log, logging.ERROR, "autopilot tick failed", product_id=str(product_id), error=f"{exc.__class__.__name__}: {exc}")
    return created


def _step_repair_dry_run_state() -> int:
    """Self-heal prospects stranded by older versions that advanced a prospect to 'outreach_sent' on a dry-run (logged)
    send: if the ledger shows only logged -- never delivered -- first contacts, put the prospect back at 'qualified'."""
    from packages.db.models import FunnelEvent, OutboundMessage, Prospect
    from packages.db.session import session_scope
    from sqlalchemy import func
    fixed = 0
    with session_scope() as session:
        stranded = session.query(Prospect).filter(Prospect.stage.in_(("outreach_sent", "follow_up")), Prospect.contact_email.isnot(None)).all()
        for p in stranded:
            email = p.contact_email.strip().lower()
            base = session.query(OutboundMessage.id).filter(OutboundMessage.company_id == p.company_id, OutboundMessage.to_email == email, OutboundMessage.kind == "first_contact")
            if base.filter(OutboundMessage.status == "logged").first() and not base.filter(OutboundMessage.status.in_(("sent", "unknown", "bounced"))).first():
                q = dict(p.qualification or {})
                q["dry_run"] = {"repaired": True}
                p.qualification, p.stage = q, "qualified"
                session.add(FunnelEvent(prospect_id=p.id, product_id=p.product_id, from_stage="outreach_sent", to_stage="qualified", agent="maintenance"))
                fixed += 1
    return fixed


STEPS: dict[str, Callable[[], int]] = {
    "repaired_dry_run_prospects": _step_repair_dry_run_state,
    "autopilot_tasks_created": _step_autopilot,
    "requeued_stuck_tasks": _step_requeue_stuck,
    "purged_sessions": _step_purge_sessions,
    "pruned_heartbeats": _step_prune_heartbeats,
    "funnel_incidents_raised": _step_funnel_checks,
}


def run_maintenance() -> dict:
    summary: dict = {}
    for name, step in STEPS.items():
        try:
            summary[name] = step()
        except Exception as exc:
            log(_log, logging.ERROR, "maintenance step failed", step=name, error=str(exc))
            summary[name] = f"error: {exc}"
    log(_log, logging.INFO, "maintenance complete", **{k: v for k, v in summary.items()})
    return summary
