from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from packages.core.services import ApprovalService
from packages.db.models import Approval, Task
from packages.db.session import session_scope

router = APIRouter()


class ApprovalOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID | None
    requested_by: str
    summary: str
    risk_level: str
    status: str
    details: dict | None = None        # what exactly is being approved, e.g. the full email text


class DecisionRequest(BaseModel):
    approved: bool


class BulkDecisionRequest(BaseModel):
    approved: bool
    ids: list[uuid.UUID] | None = None
    all_pending_outreach: bool = False
    product_id: uuid.UUID | None = None          # limit "all pending outreach" to one product


@router.get("", response_model=list[ApprovalOut])
def list_approvals(status: str | None = None) -> list[ApprovalOut]:
    with session_scope() as session:
        query = session.query(Approval)
        if status:
            query = query.filter_by(status=status)
        rows = query.order_by(Approval.created_at.desc()).limit(100).all()
        return [ApprovalOut(id=r.id, task_id=r.task_id, requested_by=r.requested_by, summary=r.summary, risk_level=r.risk_level, status=r.status, details=r.details) for r in rows]


@router.post("/{approval_id}/decide")
def decide(approval_id: uuid.UUID, request: DecisionRequest) -> dict:
    try:
        ApprovalService.decide(approval_id, approved=request.approved)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"approval_id": str(approval_id), "status": "approved" if request.approved else "rejected"}


@router.post("/bulk-decide")
def bulk_decide(request: BulkDecisionRequest) -> dict:
    """Decide many approvals at once -- e.g. after reading today's drafts, approve them all. `all_pending_outreach`
    only ever touches pending Sales outreach drafts (never a deploy or a PR)."""
    if not request.ids and not request.all_pending_outreach:
        raise HTTPException(status_code=422, detail="Provide ids, or set all_pending_outreach")
    with session_scope() as session:
        query = session.query(Approval.id).filter_by(status="pending")
        if request.ids:
            query = query.filter(Approval.id.in_(request.ids))
        else:
            query = query.filter(Approval.requested_by == "sales")
            if request.product_id:
                query = query.join(Task, Task.id == Approval.task_id).filter(Task.product_id == request.product_id)
        ids = [row[0] for row in query.all()]
    done, failed = 0, []
    for approval_id in ids:
        try:
            ApprovalService.decide(approval_id, approved=request.approved)
            done += 1
        except ValueError as exc:
            failed.append({"id": str(approval_id), "error": str(exc)})
    return {"decided": done, "approved": request.approved, "failed": failed}
