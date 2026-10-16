"""
Single entry point: audits the product's positioning/competitor configuration
(product_config.positioning / .competitors) for completeness, the same
guardrail pattern ProspectingAgent uses for ICP -- flags what's missing
rather than inventing content strategy or campaign performance data that
doesn't exist yet (no campaigns/content tables are built -- see
docs/ARCHITECTURE.md's Marketing Agent section for the target scope this is
a first, honest slice of).
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.db.models import ProductConfig
from packages.db.scoped_session import scoped_session

REQUIRED_POSITIONING_FIELDS = ("value_prop", "differentiators")


class MarketingAgent(BaseAgent):
    slug = "marketing"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        product_id = ctx.require_product()

        with scoped_session(ctx) as session:
            config = session.get(ProductConfig, product_id)
            positioning = config.positioning if config else None
            competitors = config.competitors if config else None

        missing = []
        if not positioning:
            missing.append("positioning is not configured at all")
        else:
            for field in REQUIRED_POSITIONING_FIELDS:
                if not positioning.get(field):
                    missing.append(f"positioning.{field} is missing")
        if not competitors:
            missing.append("no competitors configured")

        return {
            "status": "done",
            "ready_for_campaigns": len(missing) == 0,
            "missing": missing,
            "note": "Campaign creation/content-performance tracking requires infrastructure not yet built -- this only audits configuration readiness.",
        }
