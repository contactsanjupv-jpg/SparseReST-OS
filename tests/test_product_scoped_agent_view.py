"""
Proves the concrete claim the Company World spec depends on: the same agent
implementation, viewed under two different products, genuinely shows two
different current tasks -- not two separate agents, and not one global
status that ignores which product you're looking at.
"""
from __future__ import annotations

import uuid

import pytest

from apps.api.routers.agents import inspect_agent, list_agents
from packages.core.context import Context
from packages.core.services import AgentRunService
from packages.db.models import (
    ActivityEvent, Agent, AgentRun, AuditLog, Company, Product,
    ProductAgentEnablement, ProductConfig, Task,
)
from packages.db.session import session_scope


@pytest.fixture()
def two_product_agent_view():
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"view-test-{uuid.uuid4().hex[:8]}", name="View Test")
        session.add(company)
        session.flush()
        product_a = Product(id=uuid.uuid4(), company_id=company.id, slug=f"view-a-{uuid.uuid4().hex[:6]}", name="A", lifecycle="active")
        product_b = Product(id=uuid.uuid4(), company_id=company.id, slug=f"view-b-{uuid.uuid4().hex[:6]}", name="B", lifecycle="active")
        session.add_all([product_a, product_b])
        session.flush()
        session.add(ProductConfig(product_id=product_a.id, config_version=1))
        session.add(ProductConfig(product_id=product_b.id, config_version=1))

        sales = session.query(Agent).filter_by(slug="sales").one()
        finance = session.query(Agent).filter_by(slug="finance").one()  # company-level -- for the always-shown check
        session.add(ProductAgentEnablement(product_id=product_a.id, agent_id=sales.id, enabled=True))
        session.add(ProductAgentEnablement(product_id=product_b.id, agent_id=sales.id, enabled=False))

        task_a = Task(company_id=company.id, product_id=product_a.id, agent_id=sales.id, created_by="test", goal="Research Acme Restoration")
        task_b = Task(company_id=company.id, product_id=product_b.id, agent_id=sales.id, created_by="test", goal="Qualify XYZ Company")
        session.add_all([task_a, task_b])
        session.flush()

        ids = {
            "company_id": company.id, "product_a_id": product_a.id, "product_b_id": product_b.id,
            "sales_id": sales.id, "finance_id": finance.id, "task_a_id": task_a.id, "task_b_id": task_b.id,
        }
        session.commit()

    ctx_a = Context(company_id=ids["company_id"], agent_id=ids["sales_id"], product_id=ids["product_a_id"])
    ctx_b = Context(company_id=ids["company_id"], agent_id=ids["sales_id"], product_id=ids["product_b_id"])
    AgentRunService.start(ctx_a, ids["task_a_id"], uuid.uuid4())
    AgentRunService.start(ctx_b, ids["task_b_id"], uuid.uuid4())

    yield ids

    with session_scope() as session:
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ProductAgentEnablement).filter(ProductAgentEnablement.product_id.in_([ids["product_a_id"], ids["product_b_id"]])).delete(synchronize_session=False)
        session.query(ProductConfig).filter(ProductConfig.product_id.in_([ids["product_a_id"], ids["product_b_id"]])).delete(synchronize_session=False)
        session.query(Product).filter(Product.id.in_([ids["product_a_id"], ids["product_b_id"]])).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_same_agent_shows_different_current_task_per_product(two_product_agent_view):
    inspect_a = inspect_agent(two_product_agent_view["sales_id"], product_id=two_product_agent_view["product_a_id"])
    inspect_b = inspect_agent(two_product_agent_view["sales_id"], product_id=two_product_agent_view["product_b_id"])

    assert inspect_a.agent.id == inspect_b.agent.id, "must be the SAME agent record, not two separate ones"
    assert inspect_a.agent.current_task_goal == "Research Acme Restoration"
    assert inspect_b.agent.current_task_goal == "Qualify XYZ Company"
    assert inspect_a.agent.current_task_goal != inspect_b.agent.current_task_goal


def test_product_view_only_lists_agents_enabled_for_that_product(two_product_agent_view):
    listing_a = list_agents(product_id=two_product_agent_view["product_a_id"])
    listing_b = list_agents(product_id=two_product_agent_view["product_b_id"])

    slugs_a = {a.slug for a in listing_a}
    slugs_b = {a.slug for a in listing_b}

    assert "sales" in slugs_a, "Sales is enabled for product A -- must appear"
    assert "sales" not in slugs_b, "Sales is explicitly disabled for product B -- must NOT appear"


def test_company_level_agent_always_appears_regardless_of_product(two_product_agent_view):
    listing_a = list_agents(product_id=two_product_agent_view["product_a_id"])
    listing_b = list_agents(product_id=two_product_agent_view["product_b_id"])
    assert "finance" in {a.slug for a in listing_a}
    assert "finance" in {a.slug for a in listing_b}
