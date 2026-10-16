"""
Two entry points via context_refs['action']:

- 'record_feedback': writes one feedback row. Never itself creates or
  changes a roadmap item -- one piece of feedback is never enough (see
  docs/ARCHITECTURE.md's explicit rule: "should NOT automatically change the
  product merely because one customer asks for something").
- 'triage': groups existing feedback by category, counts real occurrences,
  and upserts a roadmap_item per category with that real count --
  `feedback_count` is a COUNT(*) result, never fabricated. Revenue impact,
  strategic importance, and competitive importance (also named in the
  architecture doc's evaluation criteria) are NOT computed here -- there is
  no revenue/customer data model yet to compute them from honestly, so they
  are left out rather than guessed.
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import EventService
from packages.db.models import Feedback, RoadmapItem
from packages.db.scoped_session import scoped_session

TRIAGE_THRESHOLD = 2  # a category needs at least this many feedback entries to become a roadmap item


class ProductManagementAgent(BaseAgent):
    slug = "product_management"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        context_refs = context_refs or {}
        action = context_refs.get("action")
        if action == "record_feedback":
            return self._record_feedback(ctx, context_refs)
        if action == "triage":
            return self._triage(ctx, context_refs)
        raise ValueError(f"ProductManagementAgent received unknown action: {action!r}")

    def _record_feedback(self, ctx: Context, context_refs: dict) -> dict:
        product_id = ctx.require_product()
        with scoped_session(ctx) as session:
            fb = Feedback(
                product_id=product_id, source=context_refs["source"],
                content=context_refs["content"], category=context_refs.get("category"),
            )
            session.add(fb)
            session.flush()
            fb_id = fb.id
        EventService.emit(ctx, "product.feedback_recorded", {"feedback_id": str(fb_id), "category": context_refs.get("category")})
        return {"status": "done", "feedback_id": str(fb_id)}

    def _triage(self, ctx: Context, context_refs: dict) -> dict:
        product_id = ctx.require_product()
        with scoped_session(ctx) as session:
            rows = session.query(Feedback).filter_by(product_id=product_id).all()
            counts: dict = {}
            for row in rows:
                category = row.category or "uncategorized"
                counts[category] = counts.get(category, 0) + 1

            proposals = []
            for category, count in counts.items():
                if count < TRIAGE_THRESHOLD:
                    continue
                item = session.query(RoadmapItem).filter_by(product_id=product_id, title=category).one_or_none()
                if item is None:
                    item = RoadmapItem(product_id=product_id, title=category, status="proposed", feedback_count=count)
                    session.add(item)
                else:
                    item.feedback_count = count
                proposals.append({"category": category, "feedback_count": count})

        proposals.sort(key=lambda p: p["feedback_count"], reverse=True)
        EventService.emit(ctx, "product.triaged", {"proposals": len(proposals)})
        return {
            "status": "done",
            "proposals": proposals,
            "below_threshold": {k: v for k, v in counts.items() if v < TRIAGE_THRESHOLD},
            "note": "feedback_count is a real count; revenue/strategic impact not computed (no data model for it yet).",
        }
