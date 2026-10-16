"""
Idempotent seed script -- safe to re-run. Creates:

- The SparseReST company (the one row in `companies` for now).
- The full 17-agent registry (CEO + 16 departments), with is_company_level
  and reports_to_slug set per docs/ARCHITECTURE.md Section 4.
- The XFlow product row, lifecycle='draft', with a config explicitly marked
  `_placeholder: true` -- NOT real ICP/positioning/pricing data. The actual
  XFlow repo (github.com/contactsanjupv-jpg/leadflow) turned out to describe
  a different product (a home-services lead-tracking CRM) than what was
  previously discussed (a real-estate Zillow-scraping tool), and that
  discrepancy hasn't been resolved yet. Seeding a placeholder rather than
  guessing means the Prospecting Agent has a product to be enabled for
  without the system quietly acting on an ICP nobody confirmed.
- A draft Product 2 stub with an empty config, so the multi-product plumbing
  has a second real product_id to prove isolation against without waiting
  on Product 2's actual repository/spec.

Run: python -m scripts.seed
"""
from __future__ import annotations

import uuid

from packages.db.models import (
    Agent,
    Company,
    Product,
    ProductAgentEnablement,
    ProductConfig,
)
from packages.db.session import session_scope

AGENTS = [
    # slug, name, department, is_company_level, reports_to_slug
    ("ceo", "CEO / Orchestrator", "executive", True, "founder"),
    ("strategy", "Strategy Agent", "executive", True, "ceo"),
    ("operations", "Operations Agent", "operations", True, "ceo"),
    ("finance", "Finance Agent", "finance", True, "ceo"),
    ("people_hr", "People / HR Agent", "people", True, "ceo"),
    ("legal", "Legal / Compliance Agent", "legal", True, "ceo"),
    ("analytics", "Analytics Agent", "analytics", True, "ceo"),
    ("growth", "Growth Agent", "growth", False, "ceo"),
    ("market_intelligence", "Market Intelligence Agent", "growth", False, "ceo"),
    ("prospecting", "Prospecting Agent", "growth", False, "growth"),
    ("sales", "Sales Agent", "growth", False, "growth"),
    ("marketing", "Marketing Agent", "growth", False, "growth"),
    ("customer_success", "Customer Success Agent", "customer_success", False, "ceo"),
    ("product_management", "Product Management Agent", "product", False, "ceo"),
    ("engineering", "Engineering Agent", "engineering", False, "ceo"),
    ("qa", "QA Agent", "engineering", False, "engineering"),
    ("devops", "DevOps Agent", "engineering", False, "engineering"),
]

PLACEHOLDER_XFLOW_CONFIG = {
    "_placeholder": True,
    "_note": (
        "ICP/positioning/pricing NOT confirmed. The repo at "
        "github.com/contactsanjupv-jpg/leadflow describes a home-services "
        "lead-tracking CRM ($499/mo), which conflicts with the real-estate "
        "Zillow-prospecting-tool description discussed earlier. Do not let "
        "the Prospecting Agent run a live campaign against this config "
        "until a human confirms which is correct and replaces this block."
    ),
    "icp": None,
    "positioning": None,
    "pricing": {"amount_usd": 499, "interval": "month", "_source": "confirmed in repo (app/pricing/page.js)"},
}


def upsert_company(session, slug: str, name: str) -> Company:
    company = session.query(Company).filter_by(slug=slug).one_or_none()
    if company is None:
        company = Company(id=uuid.uuid4(), slug=slug, name=name)
        session.add(company)
        session.flush()
    return company


def upsert_agent(session, slug, name, department, is_company_level, reports_to_slug) -> Agent:
    agent = session.query(Agent).filter_by(slug=slug).one_or_none()
    if agent is None:
        agent = Agent(
            id=uuid.uuid4(),
            slug=slug,
            name=name,
            department=department,
            is_company_level=is_company_level,
            reports_to_slug=reports_to_slug,
        )
        session.add(agent)
        session.flush()
    else:
        agent.name = name
        agent.department = department
        agent.is_company_level = is_company_level
        agent.reports_to_slug = reports_to_slug
    return agent


def upsert_product(session, company: Company, slug: str, name: str, lifecycle: str, config: dict | None) -> Product:
    product = session.query(Product).filter_by(company_id=company.id, slug=slug).one_or_none()
    if product is None:
        product = Product(id=uuid.uuid4(), company_id=company.id, slug=slug, name=name, lifecycle=lifecycle)
        session.add(product)
        session.flush()
    pc = session.query(ProductConfig).filter_by(product_id=product.id).one_or_none()
    if pc is None:
        pc = ProductConfig(product_id=product.id, config_version=1, **(config or {}))
        session.add(pc)
    return product


def enable_agents_for_product(session, product: Product, agent_slugs: list[str]) -> None:
    agents = session.query(Agent).filter(Agent.slug.in_(agent_slugs)).all()
    for agent in agents:
        row = session.query(ProductAgentEnablement).filter_by(product_id=product.id, agent_id=agent.id).one_or_none()
        if row is None:
            session.add(ProductAgentEnablement(product_id=product.id, agent_id=agent.id, enabled=True))
        else:
            row.enabled = True


def run() -> None:
    with session_scope() as session:
        company = upsert_company(session, "sparserest", "SparseReST")

        for slug, name, department, is_company_level, reports_to in AGENTS:
            upsert_agent(session, slug, name, department, is_company_level, reports_to)

        xflow = upsert_product(
            session, company, "xflow", "XFlow", lifecycle="draft",
            config={
                "icp": PLACEHOLDER_XFLOW_CONFIG["icp"],
                "positioning": PLACEHOLDER_XFLOW_CONFIG["positioning"],
                "pricing": PLACEHOLDER_XFLOW_CONFIG["pricing"],
            },
        )
        product_level_slugs = [
            "growth", "market_intelligence", "prospecting", "sales", "marketing",
            "customer_success", "product_management", "engineering", "qa", "devops",
        ]
        enable_agents_for_product(session, xflow, product_level_slugs)

        upsert_product(session, company, "product-2", "Product 2 (unspecified)", lifecycle="draft", config={})

        print(f"Seeded company={company.slug} products=[xflow(placeholder config), product-2(empty)] agents={len(AGENTS)}")


if __name__ == "__main__":
    run()
