"""
QA (real pass/fail from the pluggable TestRunner), DevOps (approval-gated
deploy, every environment), Finance (real cost rollup from audit_log, no
fabricated revenue), Marketing (real positioning-completeness audit, no
fabricated campaign data).
"""
from __future__ import annotations

import uuid

import pytest

from packages.agents.departments.devops import DevOpsAgent
from packages.agents.departments.finance import FinanceAgent
from packages.agents.departments.marketing import MarketingAgent
from packages.agents.departments.qa import QAAgent
from packages.core.context import Context
from packages.core.services import ApprovalService
from packages.db.models import (
    ActivityEvent, Agent, AgentRun, AuditLog, Company, Product,
    ProductAgentEnablement, ProductConfig, Task,
)
from packages.db.session import session_scope
from packages.tools.deploy_adapter import StubDeployAdapter
from packages.tools.test_runner import StubFailingTestRunner, StubTestRunner


@pytest.fixture()
def product_with_agents():
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"dept-test-{uuid.uuid4().hex[:8]}", name="Dept Test Co")
        session.add(company)
        session.flush()

        product = Product(id=uuid.uuid4(), company_id=company.id, slug=f"dept-test-p-{uuid.uuid4().hex[:6]}", name="Dept Test Product", lifecycle="active")
        session.add(product)
        session.flush()
        session.add(ProductConfig(product_id=product.id, config_version=1, positioning={"value_prop": "we save you time"}))

        for slug in ["qa", "devops", "marketing"]:
            agent = session.query(Agent).filter_by(slug=slug).one()
            session.add(ProductAgentEnablement(product_id=product.id, agent_id=agent.id, enabled=True))

        ids = {
            "company_id": company.id, "product_id": product.id,
            "qa_id": session.query(Agent).filter_by(slug="qa").one().id,
            "devops_id": session.query(Agent).filter_by(slug="devops").one().id,
            "finance_id": session.query(Agent).filter_by(slug="finance").one().id,
            "marketing_id": session.query(Agent).filter_by(slug="marketing").one().id,
        }
        session.commit()

    yield ids

    with session_scope() as session:
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ProductAgentEnablement).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(ProductConfig).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Product).filter_by(id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_qa_agent_reports_real_pass_result(product_with_agents):
    ctx = Context(company_id=product_with_agents["company_id"], agent_id=product_with_agents["qa_id"], product_id=product_with_agents["product_id"])
    result = QAAgent(StubTestRunner()).handle(ctx, goal="run tests", context_refs={"repo": "acme/widgets", "branch": "fix-42"})
    assert result["status"] == "done"
    assert result["passed"] is True
    assert result["failed"] == 0


def test_qa_agent_reports_real_failure_result(product_with_agents):
    ctx = Context(company_id=product_with_agents["company_id"], agent_id=product_with_agents["qa_id"], product_id=product_with_agents["product_id"])
    result = QAAgent(StubFailingTestRunner()).handle(ctx, goal="run tests", context_refs={"repo": "acme/widgets", "branch": "fix-42"})
    assert result["passed"] is False
    assert result["failed"] == 1
    assert "test_example_that_fails" in result["failures"][0]


def test_devops_agent_requires_approval_before_deploying(product_with_agents):
    ctx = Context(company_id=product_with_agents["company_id"], agent_id=product_with_agents["devops_id"], product_id=product_with_agents["product_id"])
    agent = DevOpsAgent(StubDeployAdapter())

    result_1 = agent.handle(ctx, goal="deploy", context_refs={"repo": "acme/widgets", "branch": "main", "environment": "production"})
    assert result_1["status"] == "needs_approval"
    ApprovalService.decide(uuid.UUID(result_1["approval_id"]), approved=True)

    result_2 = agent.handle(ctx, goal="deploy", context_refs={"repo": "acme/widgets", "branch": "main", "environment": "production", "approved": True})
    assert result_2["status"] == "done"
    assert result_2["deployed"] is False, "must be the stub adapter -- no real deploy without a real deploy target configured"


def test_finance_agent_rolls_up_real_recorded_cost(product_with_agents):
    from datetime import datetime, timezone
    with session_scope() as session:
        session.add(AuditLog(
            company_id=product_with_agents["company_id"], product_id=product_with_agents["product_id"],
            agent_id=product_with_agents["qa_id"], tool="test_runner", input=None, output=None,
            result="success", cost_usd=1.25, created_at=datetime.now(timezone.utc),
        ))
        session.add(AuditLog(
            company_id=product_with_agents["company_id"], product_id=product_with_agents["product_id"],
            agent_id=product_with_agents["devops_id"], tool="deploy", input=None, output=None,
            result="success", cost_usd=0.75, created_at=datetime.now(timezone.utc),
        ))

    ctx = Context(company_id=product_with_agents["company_id"], agent_id=product_with_agents["finance_id"], product_id=None)
    result = FinanceAgent().handle(ctx, goal="cost rollup", context_refs=None)

    assert result["total_cost_usd"] == 2.0
    assert result["cost_by_tool"]["test_runner"] == 1.25
    assert result["cost_by_tool"]["deploy"] == 0.75
    assert "revenue" in result["note"].lower()  # honest about what it can't report yet


def test_finance_agent_refuses_a_product_scoped_context():
    with session_scope() as session:
        finance = session.query(Agent).filter_by(slug="finance").one()
        finance_id = finance.id
    ctx = Context(company_id=uuid.uuid4(), agent_id=finance_id, product_id=uuid.uuid4())
    with pytest.raises(ValueError):
        FinanceAgent().handle(ctx, goal="cost rollup", context_refs=None)


def test_marketing_agent_flags_incomplete_positioning(product_with_agents):
    """The fixture seeds positioning with only value_prop set -- differentiators
    and competitors are missing, so this must genuinely flag both, not just
    report 'ready' because *something* is configured."""
    ctx = Context(company_id=product_with_agents["company_id"], agent_id=product_with_agents["marketing_id"], product_id=product_with_agents["product_id"])
    result = MarketingAgent().handle(ctx, goal="audit positioning", context_refs=None)
    assert result["ready_for_campaigns"] is False
    assert any("differentiators" in m for m in result["missing"])
    assert any("competitors" in m for m in result["missing"])


def test_marketing_agent_passes_when_fully_configured(product_with_agents):
    with session_scope() as session:
        config = session.get(ProductConfig, product_with_agents["product_id"])
        config.positioning = {"value_prop": "we save you time", "differentiators": ["faster", "cheaper"]}
        config.competitors = [{"name": "Acme Competitor"}]

    ctx = Context(company_id=product_with_agents["company_id"], agent_id=product_with_agents["marketing_id"], product_id=product_with_agents["product_id"])
    result = MarketingAgent().handle(ctx, goal="audit positioning", context_refs=None)
    assert result["ready_for_campaigns"] is True
    assert result["missing"] == []
