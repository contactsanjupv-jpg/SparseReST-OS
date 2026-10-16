"""
Two entry points via context_refs['action']:

- 'onboard': transitions a prospect at stage='customer' to 'onboarded'.
- 'check_health': a real, computed signal from funnel_events timestamps --
  NOT a fabricated health score. Without a product-usage data model yet
  (tracked in docs/ARCHITECTURE.md as a later integration), the only honest
  signal available is time-since-onboarding with no further progression.
  This deliberately returns 'insufficient_data' rather than inventing a
  score when there isn't one -- consistent with the no-fabricated-data rule
  applied everywhere else in this codebase (see ProspectingAgent).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import EventService
from packages.db.models import FunnelEvent, Prospect
from packages.db.scoped_session import scoped_session

STALLED_ACTIVATION_DAYS = 14


class CustomerSuccessAgent(BaseAgent):
    slug = "customer_success"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        context_refs = context_refs or {}
        action = context_refs.get("action")
        if action == "onboard":
            return self._onboard(ctx, context_refs)
        if action == "check_health":
            return self._check_health(ctx, context_refs)
        raise ValueError(f"CustomerSuccessAgent received unknown action: {action!r}")

    def _onboard(self, ctx: Context, context_refs: dict) -> dict:
        product_id = ctx.require_product()
        prospect_id = context_refs["prospect_id"]

        with scoped_session(ctx) as session:
            prospect = session.get(Prospect, prospect_id)
            if prospect is None:
                raise ValueError(f"Prospect {prospect_id} not visible to this context")
            if prospect.stage != "customer":
                return {"status": "blocked", "reason": f"Prospect is at stage '{prospect.stage}', not 'customer' -- nothing to onboard yet."}
            prospect.stage = "onboarded"
            session.add(FunnelEvent(prospect_id=prospect_id, product_id=product_id, from_stage="customer", to_stage="onboarded", agent="customer_success"))

        EventService.emit(ctx, "customer.onboarded", {"prospect_id": str(prospect_id)})
        return {"status": "done", "new_stage": "onboarded"}

    def _check_health(self, ctx: Context, context_refs: dict) -> dict:
        prospect_id = context_refs["prospect_id"]

        with scoped_session(ctx) as session:
            prospect = session.get(Prospect, prospect_id)
            if prospect is None:
                raise ValueError(f"Prospect {prospect_id} not visible to this context")
            onboarded_event = (
                session.query(FunnelEvent)
                .filter_by(prospect_id=prospect_id, to_stage="onboarded")
                .order_by(FunnelEvent.created_at.desc())
                .first()
            )
            current_stage = prospect.stage
            onboarded_at = onboarded_event.created_at if onboarded_event else None

        if current_stage == "activated":
            return {"status": "done", "finding": "activated", "note": "Customer has reached the activated stage."}

        if current_stage != "onboarded" or onboarded_at is None:
            return {"status": "done", "finding": "insufficient_data", "note": "No onboarding event recorded yet -- nothing to assess."}

        onboarded_at_aware = onboarded_at if (onboarded_at is None or onboarded_at.tzinfo is not None) else onboarded_at.replace(tzinfo=timezone.utc)
        days_since_onboarding = (datetime.now(timezone.utc) - onboarded_at_aware).days
        if days_since_onboarding >= STALLED_ACTIVATION_DAYS:
            EventService.emit(ctx, "customer.stalled_activation", {"prospect_id": str(prospect_id), "days_since_onboarding": days_since_onboarding})
            return {
                "status": "done", "finding": "stalled_activation", "days_since_onboarding": days_since_onboarding,
                "note": f"Onboarded {days_since_onboarding} days ago with no activation -- worth a founder check-in.",
            }
        return {"status": "done", "finding": "on_track", "days_since_onboarding": days_since_onboarding}
