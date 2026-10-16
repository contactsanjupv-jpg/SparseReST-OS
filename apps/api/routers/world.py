"""
Read-only feed for the Company World. Everything returned here is derived from real rows -- agent_runs,
tasks, approvals, incidents, prospects, activity_events. There is deliberately NO code path that invents
activity: an agent with no recent run is `idle`, a run that stopped reporting is `stalled` (not quietly
"working"), and the event feed is the activity_events table, nothing else.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query
from sqlalchemy import func

from packages.core.log import redact
from packages.db.models import (
    ActivityEvent, Agent, AgentRun, Approval, Incident, Product, ProductAgentEnablement, Prospect, Task,
)
from packages.db.session import session_scope

router = APIRouter()

WORKING_STATUSES = ("working", "thinking")
ACTIVE_WINDOW = timedelta(minutes=10)      # a 'working' run older than this has stopped reporting -> 'stalled'
ERROR_WINDOW = timedelta(hours=1)          # how long a failure keeps showing on the agent
PAYLOAD_STRING_LIMIT = 240


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def derive_agent_state(run: Optional[AgentRun], has_pending_approval: bool, now: datetime) -> tuple[str, Optional[datetime]]:
    """(state, since). States: working | needs_approval | stalled | error | idle."""
    if has_pending_approval:
        return "needs_approval", None
    if run is None:
        return "idle", None
    started, ended = _aware(run.started_at), _aware(run.ended_at)
    if run.status in WORKING_STATUSES:
        return ("working" if now - started <= ACTIVE_WINDOW else "stalled"), started
    if run.status == "error" and now - (ended or started) <= ERROR_WINDOW:
        return "error", ended or started
    return "idle", ended or started


def _trim(value):
    if isinstance(value, str):
        return value if len(value) <= PAYLOAD_STRING_LIMIT else value[:PAYLOAD_STRING_LIMIT] + "..."
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [_trim(v) for v in value[:10]]
    return value


@router.get("/state")
def world_state(product_id: Optional[uuid.UUID] = None) -> dict:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        agents = session.query(Agent).order_by(Agent.department, Agent.slug).all()
        if product_id is not None:
            enabled = {r.agent_id for r in session.query(ProductAgentEnablement).filter_by(product_id=product_id, enabled=True).all()}
            agents = [a for a in agents if a.is_company_level or a.id in enabled]

        product_slugs = {p.id: p.slug for p in session.query(Product).all()}
        out = []
        for agent in agents:
            scope = None if agent.is_company_level else product_id
            run_q = session.query(AgentRun).filter_by(agent_id=agent.id)
            approval_q = session.query(Task).filter_by(agent_id=agent.id, status="needs_approval")
            if scope is not None:
                run_q, approval_q = run_q.filter_by(product_id=scope), approval_q.filter_by(product_id=scope)
            run = run_q.order_by(AgentRun.started_at.desc()).first()
            waiting = approval_q.first() is not None
            state, since = derive_agent_state(run, waiting, now)

            goal = task_id = None
            if run is not None and run.task_id is not None and state in ("working", "stalled", "error"):
                task = session.get(Task, run.task_id)
                goal, task_id = (task.goal if task else None), run.task_id
            out.append({
                "id": str(agent.id), "slug": agent.slug, "name": agent.name, "department": agent.department,
                "is_company_level": agent.is_company_level, "reports_to": agent.reports_to_slug,
                "state": state, "since": since.isoformat() if since else None,
                "task_goal": goal, "task_id": str(task_id) if task_id else None,
                "product": product_slugs.get(run.product_id) if run is not None and run.product_id else None,
            })

        task_q = session.query(Task.status, func.count()).group_by(Task.status)
        stage_q = session.query(Prospect.stage, func.count()).group_by(Prospect.stage)
        incident_q = session.query(Incident.severity, func.count()).filter(Incident.status == "open").group_by(Incident.severity)
        if product_id is not None:
            task_q, stage_q = task_q.filter(Task.product_id == product_id), stage_q.filter(Prospect.product_id == product_id)
            incident_q = incident_q.filter(Incident.product_id == product_id)
        pending_approvals = session.query(func.count()).select_from(Approval).filter(Approval.status == "pending").scalar()
        cursor = session.query(func.coalesce(func.max(ActivityEvent.id), 0)).scalar()

        return {
            "as_of": now.isoformat(), "product_id": str(product_id) if product_id else None,
            "agents": out, "queue": dict(task_q.all()), "pipeline": dict(stage_q.all()),
            "open_incidents": dict(incident_q.all()), "pending_approvals": pending_approvals, "cursor": cursor,
        }


@router.get("/events")
def world_events(after_id: int = Query(0, ge=0), product_id: Optional[uuid.UUID] = None, limit: int = Query(50, ge=1, le=200)) -> dict:
    """Real activity_events after a cursor, oldest first, so the world can play them back in order."""
    with session_scope() as session:
        q = session.query(ActivityEvent, Agent.slug).outerjoin(Agent, Agent.id == ActivityEvent.agent_id).filter(ActivityEvent.id > after_id)
        if product_id is not None:
            q = q.filter((ActivityEvent.product_id == product_id) | (ActivityEvent.product_id.is_(None)))
        rows = q.order_by(ActivityEvent.id.asc()).limit(limit).all()
        events = [{
            "id": e.id, "agent": slug, "event_type": e.event_type, "task_id": str(e.task_id) if e.task_id else None,
            "payload": _trim(redact(e.payload or {})), "created_at": _aware(e.created_at).isoformat(),
        } for e, slug in rows]
    return {"events": events, "cursor": events[-1]["id"] if events else after_id}
