"""
Company-level (is_company_level=True in the seed data) -- the one agent in
this codebase whose entire job is reading across every product at once. Its
context always has product_id=None; scoped_session grants it the
cross-product read bypass because of its is_company_level flag, never
because it asked for one (see packages/db/scoped_session.py).

Computes real counts from prospects/funnel_events -- no `customers` or
`revenue_events` table exists yet in this schema (tracked in
docs/ARCHITECTURE.md as a Phase 4 addition), so revenue/MRR rollups aren't
possible yet and this agent does not fabricate them. What it CAN report
honestly -- prospect counts by stage, per product -- it reports.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.db.models import Product, Prospect
from packages.db.scoped_session import scoped_session


class AnalyticsAgent(BaseAgent):
    slug = "analytics"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        if ctx.product_id is not None:
            raise ValueError("AnalyticsAgent is company-level -- it should never be invoked with a specific product_id in context")

        with scoped_session(ctx) as session:
            products = session.query(Product).filter_by(company_id=ctx.company_id).all()
            product_names = {p.id: p.slug for p in products}
            product_slugs = [p.slug for p in products]
            prospects = session.query(Prospect).filter_by(company_id=ctx.company_id).all()

            by_product: dict = defaultdict(lambda: defaultdict(int))
            for p in prospects:
                by_product[product_names.get(p.product_id, str(p.product_id))][p.stage] += 1

        rollup = {slug: dict(stages) for slug, stages in by_product.items()}
        total_prospects = sum(sum(stages.values()) for stages in rollup.values())
        total_customers = sum(stages.get("customer", 0) + stages.get("onboarded", 0) + stages.get("activated", 0) for stages in rollup.values())

        return {
            "status": "done",
            "products_tracked": product_slugs,
            "by_product_stage_counts": rollup,
            "total_prospects": total_prospects,
            "total_customers_all_stages": total_customers,
            "note": "Revenue/MRR rollups require a customers/revenue_events table not yet built -- not fabricated here.",
        }
