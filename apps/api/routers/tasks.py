from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from apps.api.streaming import stream_task_events
from packages.agents.departments.ceo import CEOAgent
from packages.core.context import Context
from packages.core.orchestrator import default_agent_registry, run_pending_tasks
from packages.core.services import TaskService
from packages.db.models import Agent, Company, Task
from packages.db.session import session_scope

router = APIRouter()


class GoalRequest(BaseModel):
    goal: str


class TaskOut(BaseModel):
    id: uuid.UUID
    goal: str
    status: str
    agent_slug: str
    product_id: uuid.UUID | None
    attempts: int
    max_attempts: int
    last_error: str | None
    result: dict | None


def _company_and_ceo() -> tuple[uuid.UUID, uuid.UUID]:
    with session_scope() as session:
        company = session.query(Company).filter_by(slug="sparserest").one_or_none()
        if company is None:
            raise HTTPException(status_code=500, detail="SparseReST company not seeded -- run scripts/seed.py")
        ceo = session.query(Agent).filter_by(slug="ceo").one_or_none()
        if ceo is None:
            raise HTTPException(status_code=500, detail="CEO agent not seeded -- run scripts/seed.py")
        return company.id, ceo.id


@router.post("", response_model=list[TaskOut])
def submit_goal(request: GoalRequest) -> list[TaskOut]:
    """Founder submits a goal to the CEO. The CEO decomposes it into one or more
    tasks (packages/agents/departments/ceo.py); this endpoint then runs the
    resulting pending tasks synchronously via the MVP direct-dispatch path
    (see packages/core/orchestrator.py -- a real deployment runs
    run_worker_loop as a separate background process instead)."""
    company_id, ceo_id = _company_and_ceo()
    ceo_ctx = Context(company_id=company_id, agent_id=ceo_id, product_id=None)
    ceo_task_id = TaskService.create(ceo_ctx, goal=request.goal, created_by="founder")

    try:
        CEOAgent().execute(ceo_ctx, ceo_task_id, goal=request.goal)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"CEO could not decompose this goal: {exc}") from exc

    run_pending_tasks(registry=default_agent_registry())
    return _list_tasks()


@router.get("", response_model=list[TaskOut])
def list_tasks(status: str | None = None, product_id: uuid.UUID | None = None) -> list[TaskOut]:
    return _list_tasks(status=status, product_id=product_id)


def _list_tasks(status: str | None = None, product_id: uuid.UUID | None = None) -> list[TaskOut]:
    with session_scope() as session:
        query = session.query(Task)
        if status:
            query = query.filter_by(status=status)
        if product_id:
            query = query.filter_by(product_id=product_id)
        tasks = query.order_by(Task.created_at.desc()).limit(100).all()
        out = []
        for t in tasks:
            agent = session.get(Agent, t.agent_id)
            out.append(TaskOut(
                id=t.id, goal=t.goal, status=t.status, agent_slug=agent.slug if agent else "?",
                product_id=t.product_id, attempts=t.attempts, max_attempts=t.max_attempts,
                last_error=t.last_error, result=t.result,
            ))
        return out


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: uuid.UUID) -> TaskOut:
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        agent = session.get(Agent, task.agent_id)
        return TaskOut(
            id=task.id, goal=task.goal, status=task.status, agent_slug=agent.slug if agent else "?",
            product_id=task.product_id, attempts=task.attempts, max_attempts=task.max_attempts,
            last_error=task.last_error, result=task.result,
        )


@router.get("/{task_id}/stream")
def stream_task(task_id: uuid.UUID) -> StreamingResponse:
    with session_scope() as session:
        if session.get(Task, task_id) is None:
            raise HTTPException(status_code=404, detail="Task not found")
    return StreamingResponse(stream_task_events(task_id), media_type="text/event-stream")


@router.post("/{task_id}/retry", response_model=TaskOut)
def retry_task(task_id: uuid.UUID) -> TaskOut:
    """Manual retry from the Playground: only valid for a terminally-failed
    task (not one already pending/in_progress/done) -- resets the attempt
    budget so it gets a fresh full set of automatic retries, not just one
    more before immediately failing again."""
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != "failed":
            raise HTTPException(status_code=409, detail=f"Only a 'failed' task can be manually retried (current status: {task.status})")
        task.status = "pending"
        task.attempts = 0
        task.next_attempt_at = None
        task.last_error = None
    return get_task(task_id)


@router.post("/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task_id: uuid.UUID) -> TaskOut:
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        company_id, product_id, agent_id = task.company_id, task.product_id, task.agent_id

    ctx = Context(company_id=company_id, agent_id=agent_id, product_id=product_id)
    try:
        TaskService.cancel(ctx, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return get_task(task_id)


class MarkCustomerRequest(BaseModel):
    product_id: uuid.UUID
    prospect_id: uuid.UUID


def _run_founder_sales_action(product_id: uuid.UUID, prospect_id: uuid.UUID, action: str, goal: str) -> dict:
    """Shared by every founder-only, never-autonomous Sales action
    (mark_customer, mark_meeting_booked): calls the Sales agent directly,
    created_by='founder', bypassing the autonomous task queue entirely --
    no agent in this codebase can reach these actions on its own."""
    from packages.agents.departments.sales import SalesAgent
    from packages.tools.outreach import LoggingOutreachProvider

    with session_scope() as session:
        sales_agent = session.query(Agent).filter_by(slug="sales").one_or_none()
        if sales_agent is None:
            raise HTTPException(status_code=500, detail="Sales agent not seeded -- run scripts/seed.py")

    company_id, _ = _company_and_ceo()
    ctx = Context(company_id=company_id, agent_id=sales_agent.id, product_id=product_id)
    context_refs = {"action": action, "prospect_id": str(prospect_id)}
    task_id = TaskService.create(ctx, goal=goal, created_by="founder", context_refs=context_refs)
    try:
        return SalesAgent(LoggingOutreachProvider()).execute(ctx, task_id, goal=goal, context_refs=context_refs)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/mark-customer")
def mark_customer(request: MarkCustomerRequest) -> dict:
    """The pipeline transition that must always be a human decision --
    confirming a real sale actually happened."""
    return _run_founder_sales_action(request.product_id, request.prospect_id, "mark_customer", f"Mark prospect {request.prospect_id} as customer")


@router.post("/mark-meeting-booked")
def mark_meeting_booked(request: MarkCustomerRequest) -> dict:
    """Same category: only a human confirming a real meeting got scheduled
    should call this."""
    return _run_founder_sales_action(request.product_id, request.prospect_id, "mark_meeting_booked", f"Mark prospect {request.prospect_id} as meeting booked")
