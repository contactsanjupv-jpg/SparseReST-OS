"""
Proves the RLS isolation promise in docs/ARCHITECTURE.md/SECURITY.md is real,
not aspirational -- run against a real local Postgres (not SQLite: RLS is a
Postgres feature and a fake substitute here would prove nothing).

This is the first slice of the mandatory multi-product isolation test; the
full version (3 products, shared Prospecting Agent, frontend switch) lands
once the Prospecting Agent itself exists (Phase 4).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from packages.core.context import Context
from packages.db.models import Agent, Company, Product, ProductConfig, Prospect, Task
from packages.db.scoped_session import scoped_session
from packages.db.session import session_scope


@pytest.fixture()
def two_products():
    """Real company, one product-level agent, two products, one prospect each --
    created via the admin connection (session_scope), exactly like the seed script.

    Plain UUIDs are captured and yielded (not ORM objects): the setup transaction
    is committed explicitly so the separate, independently-connected scoped_session
    calls in each test can actually see these rows -- an uncommitted transaction on
    one connection is invisible to another connection, real Postgres or not.
    """
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"test-co-{uuid.uuid4().hex[:8]}", name="Test Co")
        session.add(company)
        session.flush()

        agent = Agent(
            id=uuid.uuid4(), slug=f"prospecting-{uuid.uuid4().hex[:8]}", name="Prospecting",
            department="growth", is_company_level=False, reports_to_slug="growth",
        )
        session.add(agent)
        session.flush()

        product_a = Product(id=uuid.uuid4(), company_id=company.id, slug="product-a", name="Product A", lifecycle="active")
        product_b = Product(id=uuid.uuid4(), company_id=company.id, slug="product-b", name="Product B", lifecycle="active")
        session.add_all([product_a, product_b])
        session.flush()
        session.add(ProductConfig(product_id=product_a.id, config_version=1))
        session.add(ProductConfig(product_id=product_b.id, config_version=1))

        prospect_a = Prospect(
            id=uuid.uuid4(), company_id=company.id, product_id=product_a.id,
            company_name="Acme A", dedup_key="acme-a.com",
        )
        prospect_b = Prospect(
            id=uuid.uuid4(), company_id=company.id, product_id=product_b.id,
            company_name="Acme B", dedup_key="acme-b.com",
        )
        session.add_all([prospect_a, prospect_b])

        ids = {
            "company_id": company.id, "agent_id": agent.id,
            "product_a_id": product_a.id, "product_b_id": product_b.id,
        }
        session.commit()  # make visible to the separately-connected app_role sessions used below

    yield ids

    with session_scope() as session:
        session.execute(text("DELETE FROM prospects WHERE company_id = :c"), {"c": str(ids["company_id"])})
        session.query(ProductConfig).filter(ProductConfig.product_id.in_([ids["product_a_id"], ids["product_b_id"]])).delete(synchronize_session=False)
        session.query(Product).filter(Product.id.in_([ids["product_a_id"], ids["product_b_id"]])).delete(synchronize_session=False)
        session.query(Agent).filter_by(id=ids["agent_id"]).delete()
        session.query(Company).filter_by(id=ids["company_id"]).delete()


def test_agent_scoped_to_product_a_cannot_see_product_b_prospect(two_products):
    ctx_a = Context(company_id=two_products["company_id"], agent_id=two_products["agent_id"], product_id=two_products["product_a_id"])
    with scoped_session(ctx_a) as session:
        visible = session.query(Prospect).filter_by(company_id=two_products["company_id"]).all()
        names = {p.company_name for p in visible}
        assert "Acme A" in names
        assert "Acme B" not in names, "RLS failed: product A's connection could see product B's prospect"


def test_agent_scoped_to_product_b_cannot_see_product_a_prospect(two_products):
    ctx_b = Context(company_id=two_products["company_id"], agent_id=two_products["agent_id"], product_id=two_products["product_b_id"])
    with scoped_session(ctx_b) as session:
        visible = session.query(Prospect).filter_by(company_id=two_products["company_id"]).all()
        names = {p.company_name for p in visible}
        assert "Acme B" in names
        assert "Acme A" not in names, "RLS failed: product B's connection could see product A's prospect"


def test_scoped_session_cannot_write_into_another_products_prospect_row(two_products):
    """WITH CHECK must block a cross-product write outright, not just hide the row on read."""
    ctx_a = Context(company_id=two_products["company_id"], agent_id=two_products["agent_id"], product_id=two_products["product_a_id"])
    with pytest.raises(Exception):
        with scoped_session(ctx_a) as session:
            # Attempting to insert a prospect under product_b while scoped to product_a.
            session.add(Prospect(
                id=uuid.uuid4(), company_id=two_products["company_id"], product_id=two_products["product_b_id"],
                company_name="Sneaky Cross-Product Insert", dedup_key="sneaky.com",
            ))
            session.flush()


def test_product_level_agent_requires_a_product_id_in_context(two_products):
    """A product-level agent with product_id=None is a bug in task assignment, not a
    valid state -- Context.require_product / scoped_session must refuse it outright."""
    ctx_ambiguous = Context(company_id=two_products["company_id"], agent_id=two_products["agent_id"], product_id=None)
    with pytest.raises(ValueError):
        with scoped_session(ctx_ambiguous):
            pass


def test_task_with_null_product_id_is_company_scope_not_agent_scope(two_products):
    """Confirms the actual seeded CEO agent (is_company_level=True) can open a
    company-scoped session (product_id=None) without raising -- this is the
    'company-level work' path, exercised against the real seeded registry."""
    with session_scope() as session:
        ceo = session.query(Agent).filter_by(slug="ceo").one()
        ceo_id = ceo.id
    ctx = Context(company_id=two_products["company_id"], agent_id=ceo_id, product_id=None)
    with scoped_session(ctx) as session:
        # Should not raise, and should see rows across both products (cross-product read allowed for company-level agents).
        visible = session.query(Prospect).filter_by(company_id=two_products["company_id"]).all()
        names = {p.company_name for p in visible}
        assert {"Acme A", "Acme B"}.issubset(names), "Company-level agent should see rollups across both products"
