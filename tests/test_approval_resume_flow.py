"""
This is the fix for a real gap: approval-gated agents (Sales, Engineering,
DevOps, People/HR) previously returned {'status': 'needs_approval', ...} but
BaseAgent.execute() still marked the underlying task 'done' -- meaning
nothing was ever actually paused at the database level, and approving a
request via the API had no way to resume anything. This test proves the
fixed behavior: the task genuinely sits at 'needs_approval', and deciding it
via ApprovalService.decide actually requeues (or terminally fails) it.
"""
from __future__ import annotations

import uuid

import pytest

from packages.agents.departments.sales import SalesAgent
from packages.core.context import Context
from packages.core.orchestrator import run_one_worker_cycle
from packages.core.services import ApprovalService, TaskService
from packages.db.models import (
    ActivityEvent, Agent, AgentRun, Approval, AuditLog, Company, FunnelEvent,
    Product, ProductAgentEnablement, ProductConfig, Prospect, Task,
)
from packages.db.session import session_scope
from packages.tools.outreach import LoggingOutreachProvider


@pytest.fixture()
def approval_flow_setup():
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"approval-flow-{uuid.uuid4().hex[:8]}", name="Approval Flow Test")
        session.add(company)
        session.flush()
        product = Product(id=uuid.uuid4(), company_id=company.id, slug=f"approval-flow-p-{uuid.uuid4().hex[:6]}", name="Product", lifecycle="active")
        session.add(product)
        session.flush()
        session.add(ProductConfig(product_id=product.id, config_version=1, sales={"physical_address": "123 Test St, Testville, TS 00000"}))
        sales_agent = session.query(Agent).filter_by(slug="sales").one()
        session.add(ProductAgentEnablement(product_id=product.id, agent_id=sales_agent.id, enabled=True))

        prospect = Prospect(
            company_id=company.id, product_id=product.id, company_name="Resume Test Co",
            contact_email="owner@resumetest.invalid", contact_name="Pat", dedup_key="resumetest.invalid", stage="qualified",
        )
        session.add(prospect)
        session.flush()

        ids = {"company_id": company.id, "product_id": product.id, "sales_agent_id": sales_agent.id, "prospect_id": prospect.id}
        session.commit()

    yield ids

    with session_scope() as session:
        session.query(FunnelEvent).filter_by(prospect_id=ids["prospect_id"]).delete(synchronize_session=False)
        session.query(Prospect).filter_by(id=ids["prospect_id"]).delete(synchronize_session=False)
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ProductAgentEnablement).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(ProductConfig).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Product).filter_by(id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_task_genuinely_pauses_at_needs_approval_not_done(approval_flow_setup):
    ctx = Context(company_id=approval_flow_setup["company_id"], agent_id=approval_flow_setup["sales_agent_id"], product_id=approval_flow_setup["product_id"])
    task_id = TaskService.create(ctx, goal="draft outreach", created_by="test", context_refs={"action": "draft_and_send", "prospect_id": str(approval_flow_setup["prospect_id"])})

    agent = SalesAgent(LoggingOutreachProvider())
    result = agent.execute(ctx, task_id, goal="draft outreach", context_refs={"action": "draft_and_send", "prospect_id": str(approval_flow_setup["prospect_id"])})
    assert result["status"] == "needs_approval"

    with session_scope() as session:
        task = session.get(Task, task_id)
        assert task.status == "needs_approval", "the task itself must reflect the pause, not silently read 'done'"


def test_approving_via_approval_service_resumes_and_completes_the_task(approval_flow_setup):
    ctx = Context(company_id=approval_flow_setup["company_id"], agent_id=approval_flow_setup["sales_agent_id"], product_id=approval_flow_setup["product_id"])
    context_refs = {"action": "draft_and_send", "prospect_id": str(approval_flow_setup["prospect_id"])}
    task_id = TaskService.create(ctx, goal="draft outreach", created_by="test", context_refs=context_refs)

    agent = SalesAgent(LoggingOutreachProvider())
    result_1 = agent.execute(ctx, task_id, goal="draft outreach", context_refs=context_refs)
    approval_id = uuid.UUID(result_1["approval_id"])

    # The founder approves via the real service the API calls -- not a manually
    # reconstructed context_refs dict in the test.
    ApprovalService.decide(approval_id, approved=True)

    with session_scope() as session:
        task = session.get(Task, task_id)
        assert task.status == "pending", "approving must requeue the task, not leave it stuck"
        assert task.context_refs.get("approved") is True

    # The worker picks it back up exactly like any other pending task -- no
    # special-casing needed in the queue or orchestrator for the resume path.
    worker_result = run_one_worker_cycle("resume-worker", registry={"sales": agent}, product_id=approval_flow_setup["product_id"])
    assert worker_result["status"] == "done"
    assert worker_result["result"]["status"] == "done"
    assert worker_result["result"]["sent"] is False  # LoggingOutreachProvider -- no real send happened

    with session_scope() as session:
        task = session.get(Task, task_id)
        assert task.status == "done"


def test_rejecting_via_approval_service_fails_the_task_with_a_clear_reason(approval_flow_setup):
    ctx = Context(company_id=approval_flow_setup["company_id"], agent_id=approval_flow_setup["sales_agent_id"], product_id=approval_flow_setup["product_id"])
    context_refs = {"action": "draft_and_send", "prospect_id": str(approval_flow_setup["prospect_id"])}
    task_id = TaskService.create(ctx, goal="draft outreach", created_by="test", context_refs=context_refs)

    agent = SalesAgent(LoggingOutreachProvider())
    result_1 = agent.execute(ctx, task_id, goal="draft outreach", context_refs=context_refs)
    approval_id = uuid.UUID(result_1["approval_id"])

    ApprovalService.decide(approval_id, approved=False)

    with session_scope() as session:
        task = session.get(Task, task_id)
        assert task.status == "failed"
        assert "rejected" in task.last_error.lower()


def test_deciding_an_already_decided_approval_is_refused(approval_flow_setup):
    ctx = Context(company_id=approval_flow_setup["company_id"], agent_id=approval_flow_setup["sales_agent_id"], product_id=approval_flow_setup["product_id"])
    context_refs = {"action": "draft_and_send", "prospect_id": str(approval_flow_setup["prospect_id"])}
    task_id = TaskService.create(ctx, goal="draft outreach", created_by="test", context_refs=context_refs)

    agent = SalesAgent(LoggingOutreachProvider())
    result_1 = agent.execute(ctx, task_id, goal="draft outreach", context_refs=context_refs)
    approval_id = uuid.UUID(result_1["approval_id"])

    ApprovalService.decide(approval_id, approved=True)
    with pytest.raises(ValueError):
        ApprovalService.decide(approval_id, approved=False)
