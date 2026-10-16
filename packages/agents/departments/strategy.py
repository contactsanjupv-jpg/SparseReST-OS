"""
Company-level. Single entry point: compares real qualified-prospect counts
across all active products and flags whichever has the lowest volume as
needing attention. This is a deliberately simple, explainable heuristic --
not a substitute for real strategic judgment, and not dressed up as one.
Real LLM-based strategic synthesis (model_tier='strong') is the natural
upgrade once ANTHROPIC_API_KEY is live; this is the $0-cost, always-available
fallback, in the same spirit as CEOAgent's pattern-matched decomposition.
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.db.models import Product, Prospect
from packages.db.scoped_session import scoped_session

QUALIFIED_STAGES = {"qualified", "researched", "personalized", "outreach_sent", "reply_received", "reply_classified", "customer", "onboarded", "activated"}


class StrategyAgent(BaseAgent):
    slug = "strategy"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        if ctx.product_id is not None:
            raise ValueError("StrategyAgent is company-level -- it should never be invoked with a specific product_id in context")

        with scoped_session(ctx) as session:
            products = session.query(Product).filter_by(company_id=ctx.company_id, lifecycle="active").all()
            product_info = [(p.id, p.slug) for p in products]
            counts = {}
            for product_id, slug in product_info:
                counts[slug] = session.query(Prospect).filter(
                    Prospect.product_id == product_id, Prospect.stage.in_(QUALIFIED_STAGES)
                ).count()

        if not counts:
            return {"status": "done", "finding": "no_active_products", "note": "No products are currently 'active' -- nothing to compare."}

        lowest_slug = min(counts, key=counts.get)
        return {
            "status": "done",
            "qualified_prospect_counts_by_product": counts,
            "lowest_volume_product": lowest_slug,
            "note": f"'{lowest_slug}' has the fewest qualified prospects of your active products -- worth checking whether its ICP, channels, or messaging need attention.",
        }
