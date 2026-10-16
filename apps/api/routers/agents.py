from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from packages.db.models import ActivityEvent, Agent, AgentRun, AuditLog, ProductAgentEnablement, Task
from packages.db.session import session_scope

router = APIRouter()


class AgentOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    department: str
    is_company_level: bool
    reports_to_slug: str | None
    current_status: str            # derived from the most recent agent_run, or 'idle' if none
    current_product_id: uuid.UUID | None = None  # which product that status/task belongs to, when scoped
    current_task_goal: str | None = None


class ToolCallOut(BaseModel):
    tool: str
    result: str
    input: dict | None
    output: dict | None
    cost_usd: float
    created_at: str


class AgentInspectOut(BaseModel):
    agent: AgentOut
    recent_tasks: list[dict]
    recent_tool_calls: list[ToolCallOut]
    recent_activity: list[dict]


def _latest_run_status(session, agent_id: uuid.UUID, product_id: uuid.UUID | None):
    """product_id given: the product-scoped truth (what THIS product's view of
    this agent looks like -- the same Sales Agent shows a different current
    task under XFlow vs Product 2, proving the shared-agent/per-product-context
    design is real, not just documented). product_id=None: the agent's overall
    most recent activity, for the company-wide/'All Company' view."""
    query = session.query(AgentRun).filter_by(agent_id=agent_id)
    if product_id is not None:
        query = query.filter_by(product_id=product_id)
    run = query.order_by(AgentRun.started_at.desc()).first()
    if run is None:
        return "idle", None, None
    task_goal = None
    if run.task_id is not None:
        task = session.get(Task, run.task_id)
        task_goal = task.goal if task else None
    return run.status, run.product_id, task_goal


@router.get("", response_model=list[AgentOut])
def list_agents(department: str | None = None, product_id: uuid.UUID | None = None) -> list[AgentOut]:
    with session_scope() as session:
        query = session.query(Agent)
        if department:
            query = query.filter_by(department=department)
        agents = query.order_by(Agent.department, Agent.slug).all()

        if product_id is not None:
            enabled_ids = {
                row.agent_id for row in
                session.query(ProductAgentEnablement).filter_by(product_id=product_id, enabled=True).all()
            }
            agents = [a for a in agents if a.is_company_level or a.id in enabled_ids]

        out = []
        for agent in agents:
            # Company-level agents always show their own (company-wide) activity, even in a
            # product-scoped view -- they operate above the product layer by design.
            scope = None if agent.is_company_level else product_id
            status, run_product_id, task_goal = _latest_run_status(session, agent.id, scope)
            out.append(AgentOut(
                id=agent.id, slug=agent.slug, name=agent.name, department=agent.department,
                is_company_level=agent.is_company_level, reports_to_slug=agent.reports_to_slug,
                current_status=status, current_product_id=run_product_id, current_task_goal=task_goal,
            ))
        return out


@router.get("/{agent_id}", response_model=AgentInspectOut)
def inspect_agent(agent_id: uuid.UUID, product_id: uuid.UUID | None = None) -> AgentInspectOut:
    with session_scope() as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        scope = None if agent.is_company_level else product_id
        status, run_product_id, task_goal = _latest_run_status(session, agent_id, scope)

        tasks_query = session.query(Task).filter_by(agent_id=agent_id)
        tool_calls_query = session.query(AuditLog).filter_by(agent_id=agent_id)
        activity_query = session.query(ActivityEvent).filter_by(agent_id=agent_id)
        if product_id is not None:
            tasks_query = tasks_query.filter_by(product_id=product_id)
            tool_calls_query = tool_calls_query.filter_by(product_id=product_id)
            activity_query = activity_query.filter_by(product_id=product_id)

        tasks = tasks_query.order_by(Task.created_at.desc()).limit(20).all()
        tool_calls = tool_calls_query.order_by(AuditLog.created_at.desc()).limit(20).all()
        activity = activity_query.order_by(ActivityEvent.created_at.desc()).limit(30).all()

        return AgentInspectOut(
            agent=AgentOut(
                id=agent.id, slug=agent.slug, name=agent.name, department=agent.department,
                is_company_level=agent.is_company_level, reports_to_slug=agent.reports_to_slug,
                current_status=status, current_product_id=run_product_id, current_task_goal=task_goal,
            ),
            recent_tasks=[{"id": str(t.id), "goal": t.goal, "status": t.status, "attempts": t.attempts, "created_at": t.created_at.isoformat()} for t in tasks],
            recent_tool_calls=[ToolCallOut(tool=c.tool, result=c.result, input=c.input, output=c.output, cost_usd=float(c.cost_usd or 0), created_at=c.created_at.isoformat()) for c in tool_calls],
            recent_activity=[{"event_type": e.event_type, "payload": e.payload, "created_at": e.created_at.isoformat()} for e in activity],
        )
