from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func

from apps.api.routers.health import schema_state
from packages.core.config import get_settings
from packages.core.maintenance import run_maintenance
from packages.core.workers import workers_health
from packages.db.models import Approval, Incident, Task
from packages.db.session import session_scope

router = APIRouter()


@router.get("/workers")
def workers() -> dict:
    return workers_health(get_settings().worker_stale_after_seconds)


@router.get("/status")
def status() -> dict:
    """One call for the 'is the company actually running?' panel. Contains no secrets -- only whether
    each integration is configured and which mode it runs in."""
    s = get_settings()
    with session_scope() as session:
        queue = dict(session.query(Task.status, func.count()).group_by(Task.status).all())
        incidents = dict(session.query(Incident.severity, func.count()).filter(Incident.status == "open").group_by(Incident.severity).all())
        pending_approvals = session.query(func.count()).select_from(Approval).filter(Approval.status == "pending").scalar()
    try:
        schema = schema_state()
    except Exception:
        schema = {"up_to_date": False}

    w = workers_health(s.worker_stale_after_seconds)
    return {
        "environment": s.environment,
        "schema_up_to_date": schema.get("up_to_date", False),
        "workers_alive": w["workers_alive"],
        "inbound_alive": w["inbound_alive"],
        "queue": queue,
        "open_incidents": incidents,
        "pending_approvals": pending_approvals,
        "integrations": {
            "llm": "claude" if s.has_live_llm else "deterministic-stub",
            "enrichment": s.enrichment_mode,
            "outreach": s.outreach_mode if (s.outreach_mode == "smtp" and s.smtp_host and s.smtp_username and s.smtp_password and s.smtp_from_email) else "stub",
            "inbound_imap": bool(s.imap_host and (s.imap_username or s.smtp_username) and (s.imap_password or s.smtp_password)),
            "notifications": "slack" if s.has_live_notifications else "console",
        },
    }


@router.post("/maintenance/run")
def maintenance() -> dict:
    return run_maintenance()
