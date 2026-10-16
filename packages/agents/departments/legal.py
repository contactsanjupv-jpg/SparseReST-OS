"""
Single entry point: reviews a piece of outreach/marketing copy against
product_config.sales['disallowed_claims'] (a plain list of phrases the
founder has flagged as not okay to say -- configured via the product wizard,
not invented by this agent). This is a real, mechanical substring check, not
a legal opinion -- it flags matches for a human to judge, it does not itself
decide something is or isn't compliant.
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import EventService
from packages.db.models import ProductConfig
from packages.db.scoped_session import scoped_session


class LegalAgent(BaseAgent):
    slug = "legal"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        context_refs = context_refs or {}
        text = context_refs["text"]
        product_id = ctx.require_product()

        with scoped_session(ctx) as session:
            config = session.get(ProductConfig, product_id)
            sales_config = config.sales if config else None

        disallowed = (sales_config or {}).get("disallowed_claims", [])
        flagged = [phrase for phrase in disallowed if phrase.lower() in text.lower()]

        if flagged:
            EventService.emit(ctx, "legal.flagged_copy", {"flagged_phrases": flagged})

        return {
            "status": "done",
            "clear": len(flagged) == 0,
            "flagged_phrases": flagged,
            "note": (
                "Mechanical substring check against configured disallowed claims only -- "
                "not a legal opinion. No disallowed_claims configured means nothing to check "
                "against, not that the copy is confirmed compliant." if not disallowed else None
            ),
        }
