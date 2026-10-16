"""
Engineering (stub GitHub adapter, approval-gated PR), Customer Success (real
onboarding transition + a computed, non-fabricated health signal), and
Analytics (company-level cross-product rollup, exercising the legitimate
cross-product read path from the RLS design).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from packages.agents.departments.analytics import AnalyticsAgent
from packages.agents.departments.customer_success import CustomerSuccessAgent
from packages.agents.departments.engineering import EngineeringAgent
from packages.core.context import Context
from packages.core.services import ApprovalService, TaskService
from packages.db.models import (
    Agent, ActivityEvent, AgentRun, AuditLog, Company, FunnelEvent, Product,
    ProductAgentEnablement, ProductConfig, Prospect, Task,
)
from packages.db.session import session_scope
from packages.tools.github_adapter import StubGitHubAdapter


@pytest.fixture()
def two_product_company():
    """One company, two products -- enough to prove Analytics genuinely rolls
    up across products rather than reporting just one."""
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"agents-test-{uuid.uuid4().hex[:8]}", name="Agents Test Co")
        session.add(company)
        session.flush()

        product_a = Product(id=uuid.uuid4(), company_id=company.id, slug=f"agtest-a-{uuid.uuid4().hex[:6]}", name="A", lifecycle="active")
        product_b = Product(id=uuid.uuid4(), company_id=company.id, slug=f"agtest-b-{uuid.uuid4().hex[:6]}", name="B", lifecycle="active")
        session.add_all([product_a, product_b])
        session.flush()
        session.add(ProductConfig(product_id=product_a.id, config_version=1))
        session.add(ProductConfig(product_id=product_b.id, config_version=1))

        for slug in ["analytics", "customer_success", "engineering"]:
            agent = session.query(Agent).filter_by(slug=slug).one()
            if not agent.is_company_level:
                session.add(ProductAgentEnablement(product_id=product_a.id, agent_id=agent.id, enabled=True))
                session.add(ProductAgentEnablement(product_id=product_b.id, agent_id=agent.id, enabled=True))

        ids = {
            "company_id": company.id, "product_a_id": product_a.id, "product_b_id": product_b.id,
            "analytics_id": session.query(Agent).filter_by(slug="analytics").one().id,
            "customer_success_id": session.query(Agent).filter_by(slug="customer_success").one().id,
            "engineering_id": session.query(Agent).filter_by(slug="engineering").one().id,
        }
        session.commit()

    yield ids

    with session_scope() as session:
        for pid in (ids["product_a_id"], ids["product_b_id"]):
            prospect_ids = [p.id for p in session.query(Prospect).filter_by(product_id=pid).all()]
            if prospect_ids:
                session.query(FunnelEvent).filter(FunnelEvent.prospect_id.in_(prospect_ids)).delete(synchronize_session=False)
            session.query(Prospect).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(ProductAgentEnablement).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(ProductConfig).filter_by(product_id=pid).delete(synchronize_session=False)
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Product).filter(Product.id.in_([ids["product_a_id"], ids["product_b_id"]])).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_engineering_agent_plans_from_a_real_stub_issue(two_product_company):
    ctx = Context(company_id=two_product_company["company_id"], agent_id=two_product_company["engineering_id"], product_id=two_product_company["product_a_id"])
    agent = EngineeringAgent(StubGitHubAdapter())
    result = agent.handle(ctx, goal="plan fix", context_refs={"action": "plan", "repo": "acme/widgets", "issue_number": 42})
    assert result["status"] == "done"
    assert "42" in result["plan"]["issue_title"]
    assert len(result["plan"]["steps"]) >= 3


def test_engineering_agent_pr_requires_approval_then_opens_stub_pr(two_product_company):
    ctx = Context(company_id=two_product_company["company_id"], agent_id=two_product_company["engineering_id"], product_id=two_product_company["product_a_id"])
    agent = EngineeringAgent(StubGitHubAdapter())

    result_1 = agent.handle(ctx, goal="open pr", context_refs={"action": "open_pr", "repo": "acme/widgets", "branch_name": "fix-42"})
    assert result_1["status"] == "needs_approval"
    ApprovalService.decide(uuid.UUID(result_1["approval_id"]), approved=True)

    result_2 = agent.handle(ctx, goal="open pr", context_refs={"action": "open_pr", "repo": "acme/widgets", "branch_name": "fix-42", "approved": True})
    assert result_2["status"] == "done"
    assert "STUB" in result_2["pr_url"], "must be the stub adapter's fake PR, never a real one without real credentials"


def test_customer_success_onboarding_and_stalled_activation(two_product_company):
    with session_scope() as session:
        prospect = Prospect(
            company_id=two_product_company["company_id"], product_id=two_product_company["product_a_id"],
            company_name="New Customer Co", dedup_key="newcustomer.invalid", stage="customer",
        )
        session.add(prospect)
        session.flush()
        prospect_id = prospect.id

    ctx = Context(company_id=two_product_company["company_id"], agent_id=two_product_company["customer_success_id"], product_id=two_product_company["product_a_id"])
    agent = CustomerSuccessAgent()

    onboard_result = agent.handle(ctx, goal="onboard", context_refs={"action": "onboard", "prospect_id": str(prospect_id)})
    assert onboard_result["status"] == "done"
    assert onboard_result["new_stage"] == "onboarded"

    # Backdate the onboarding FunnelEvent to simulate 20 days having passed --
    # avoids a real 20-day sleep in the test suite.
    with session_scope() as session:
        event = session.query(FunnelEvent).filter_by(prospect_id=prospect_id, to_stage="onboarded").one()
        event.created_at = datetime.now(timezone.utc) - timedelta(days=20)

    health_result = agent.handle(ctx, goal="check health", context_refs={"action": "check_health", "prospect_id": str(prospect_id)})
    assert health_result["finding"] == "stalled_activation"
    assert health_result["days_since_onboarding"] >= 20

    with session_scope() as session:
        session.query(FunnelEvent).filter_by(prospect_id=prospect_id).delete()
        session.query(Prospect).filter_by(id=prospect_id).delete()


def test_customer_success_onboarding_blocked_at_wrong_stage(two_product_company):
    with session_scope() as session:
        prospect = Prospect(
            company_id=two_product_company["company_id"], product_id=two_product_company["product_a_id"],
            company_name="Not Yet A Customer Co", dedup_key="notyet.invalid", stage="qualified",
        )
        session.add(prospect)
        session.flush()
        prospect_id = prospect.id

    ctx = Context(company_id=two_product_company["company_id"], agent_id=two_product_company["customer_success_id"], product_id=two_product_company["product_a_id"])
    result = CustomerSuccessAgent().handle(ctx, goal="onboard", context_refs={"action": "onboard", "prospect_id": str(prospect_id)})
    assert result["status"] == "blocked"

    with session_scope() as session:
        session.query(Prospect).filter_by(id=prospect_id).delete()


def test_analytics_agent_rolls_up_across_both_products(two_product_company):
    with session_scope() as session:
        session.add(Prospect(company_id=two_product_company["company_id"], product_id=two_product_company["product_a_id"], company_name="A1", dedup_key="a1.invalid", stage="qualified"))
        session.add(Prospect(company_id=two_product_company["company_id"], product_id=two_product_company["product_a_id"], company_name="A2", dedup_key="a2.invalid", stage="customer"))
        session.add(Prospect(company_id=two_product_company["company_id"], product_id=two_product_company["product_b_id"], company_name="B1", dedup_key="b1.invalid", stage="qualified"))

    ctx = Context(company_id=two_product_company["company_id"], agent_id=two_product_company["analytics_id"], product_id=None)
    result = AnalyticsAgent().handle(ctx, goal="rollup", context_refs=None)

    assert result["total_prospects"] == 3
    assert result["total_customers_all_stages"] == 1
    assert len(result["by_product_stage_counts"]) == 2, "must see BOTH products -- proves the cross-product read path is real"


def test_analytics_agent_refuses_a_product_scoped_context():
    """A company-level agent invoked with a specific product_id is a bug in
    task assignment (should have been product_id=None) -- must fail loudly."""
    with session_scope() as session:
        analytics = session.query(Agent).filter_by(slug="analytics").one()
        analytics_id = analytics.id
    fake_product_id = uuid.uuid4()
    ctx = Context(company_id=uuid.uuid4(), agent_id=analytics_id, product_id=fake_product_id)
    with pytest.raises(ValueError):
        AnalyticsAgent().handle(ctx, goal="rollup", context_refs=None)
