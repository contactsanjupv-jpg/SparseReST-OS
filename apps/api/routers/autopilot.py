"""Autopilot control plane: the ON/OFF switch, mode, safety resume, limits, reports, suppressions."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from packages.core import autopilot, campaign, outbound, reporting
from packages.core.autopilot_config import ConfigError
from packages.db.models import GrowthReport, Product, Suppression
from packages.db.session import session_scope

router = APIRouter()


class EnableRequest(BaseModel):
    mode: str = Field("approval", pattern="^(approval|auto)$")
    ack_auto: bool = False


class ConfigPatch(BaseModel):
    settings: dict


class GoalRequest(BaseModel):
    customers: int = Field(ge=1, le=1000)
    days: int = Field(ge=1, le=365)


class ExtendRequest(BaseModel):
    days: int = Field(ge=1, le=365)


class SuppressRequest(BaseModel):
    kind: str = Field(pattern="^(email|domain)$")
    value: str = Field(min_length=3, max_length=254)
    reason: str = "manual"


def _company(product_id: uuid.UUID) -> uuid.UUID:
    with session_scope() as session:
        product = session.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        return product.company_id


@router.get("/{product_id}/status")
def status(product_id: uuid.UUID) -> dict:
    company_id = _company(product_id)
    return {**autopilot.get_status(company_id, product_id), "readiness": autopilot.readiness(company_id, product_id)}


@router.post("/{product_id}/enable")
def enable(product_id: uuid.UUID, body: EnableRequest) -> dict:
    try:
        return autopilot.enable(_company(product_id), product_id, mode=body.mode, ack_auto=body.ack_auto, by="founder")
    except autopilot.AutopilotError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{product_id}/disable")
def disable(product_id: uuid.UUID) -> dict:
    return autopilot.disable(_company(product_id), product_id)


@router.post("/{product_id}/resume")
def resume(product_id: uuid.UUID) -> dict:
    """Clears a safety-breaker pause. Deliberately a separate, explicit action from `enable`."""
    company_id = _company(product_id)
    if not outbound.resume(product_id):
        raise HTTPException(status_code=409, detail="Sending is not paused")
    return autopilot.get_status(company_id, product_id)


@router.patch("/{product_id}/config")
def patch_config(product_id: uuid.UUID, body: ConfigPatch) -> dict:
    try:
        return autopilot.update_config(_company(product_id), product_id, body.settings)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/{product_id}/goal")
def get_goal(product_id: uuid.UUID) -> dict:
    _company(product_id)
    return campaign.progress(product_id)


@router.post("/{product_id}/goal")
def set_goal(product_id: uuid.UUID, body: GoalRequest) -> dict:
    """Define the campaign ("N customers in D days"). The clock starts when autopilot is enabled."""
    _company(product_id)
    try:
        return campaign.set_goal(product_id, body.customers, body.days)
    except campaign.GoalError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/{product_id}/goal/extend")
def extend_goal(product_id: uuid.UUID, body: ExtendRequest) -> dict:
    _company(product_id)
    try:
        return campaign.extend(product_id, body.days)
    except campaign.GoalError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{product_id}/tick")
def tick(product_id: uuid.UUID) -> dict:
    return autopilot.tick(_company(product_id), product_id)


@router.get("/{product_id}/reports")
def list_reports(product_id: uuid.UUID, period: Optional[str] = None, limit: int = 10) -> list[dict]:
    _company(product_id)
    with session_scope() as session:
        q = session.query(GrowthReport).filter_by(product_id=product_id)
        if period:
            q = q.filter_by(period=period)
        return [{"id": str(r.id), "period": r.period, "period_start": r.period_start.isoformat(), "summary": r.summary,
                 "metrics": r.metrics, "bottlenecks": r.bottlenecks} for r in q.order_by(GrowthReport.period_start.desc()).limit(min(limit, 50)).all()]


@router.post("/suppressions", status_code=201)
def add_suppression(body: SuppressRequest, product_id: uuid.UUID) -> dict:
    outbound.suppress(_company(product_id), body.kind, body.value, body.reason, "founder")
    return {"suppressed": body.value.lower(), "kind": body.kind}


@router.get("/suppressions")
def list_suppressions(product_id: uuid.UUID, limit: int = 100) -> list[dict]:
    company_id = _company(product_id)
    with session_scope() as session:
        rows = session.query(Suppression).filter_by(company_id=company_id).order_by(Suppression.created_at.desc()).limit(min(limit, 500)).all()
        return [{"kind": r.kind, "value": r.value, "reason": r.reason, "source": r.source, "at": r.created_at.isoformat()} for r in rows]
