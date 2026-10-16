"""
Product-level. Single entry point: audits each configured competitor
(product_config.competitors) for the fields needed to actually compete
against it -- name, differentiation, and at least one of url/pricing. This
is distinct from MarketingAgent's overall positioning check: this looks at
each competitor individually and reports which ones are under-researched.

No web research is performed here yet (packages/tools/ has no web-research
adapter built -- tracked in STATUS.md); this only audits what's already been
entered, honestly, rather than fabricating competitor intelligence.
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.db.models import ProductConfig
from packages.db.scoped_session import scoped_session


class MarketIntelligenceAgent(BaseAgent):
    slug = "market_intelligence"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        product_id = ctx.require_product()

        with scoped_session(ctx) as session:
            config = session.get(ProductConfig, product_id)
            competitors = config.competitors if config else None

        if not competitors:
            return {"status": "done", "finding": "no_competitors_configured", "under_researched": [], "well_researched": []}

        under_researched, well_researched = [], []
        for competitor in competitors:
            name = competitor.get("name", "(unnamed)")
            has_differentiation = bool(competitor.get("differentiation"))
            has_reference = bool(competitor.get("url") or competitor.get("pricing"))
            if has_differentiation and has_reference:
                well_researched.append(name)
            else:
                missing = []
                if not has_differentiation:
                    missing.append("differentiation")
                if not has_reference:
                    missing.append("url or pricing")
                under_researched.append({"name": name, "missing": missing})

        return {
            "status": "done",
            "well_researched": well_researched,
            "under_researched": under_researched,
            "note": "Audits configured competitor data only -- no live web research performed (no research tool wired in yet).",
        }
