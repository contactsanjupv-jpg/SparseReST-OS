from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from packages.core.incidents import IncidentService
from packages.db.models import Incident
from packages.db.session import session_scope

router = APIRouter()


class IncidentOut(BaseModel):
    id: uuid.UUID
    severity: str
    title: str
    cause: str | None
    impact: str | None
    action_required: str | None
    status: str
    company_id: uuid.UUID
    product_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    task_id: uuid.UUID | None
    created_at: str


@router.get("", response_model=list[IncidentOut])
def list_incidents(status: str | None = None, severity: str | None = None) -> list[IncidentOut]:
    with session_scope() as session:
        query = session.query(Incident)
        if status:
            query = query.filter_by(status=status)
        if severity:
            query = query.filter_by(severity=severity)
        rows = query.order_by(Incident.created_at.desc()).limit(200).all()
        return [
            IncidentOut(
                id=r.id, severity=r.severity, title=r.title, cause=r.cause, impact=r.impact,
                action_required=r.action_required, status=r.status, company_id=r.company_id,
                product_id=r.product_id, agent_id=r.agent_id, task_id=r.task_id, created_at=r.created_at.isoformat(),
            )
            for r in rows
        ]


@router.post("/{incident_id}/resolve")
def resolve_incident(incident_id: uuid.UUID) -> dict:
    try:
        IncidentService.resolve(incident_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"incident_id": str(incident_id), "status": "resolved"}
