"""
Two entry points, dispatched via context_refs['action'] (default:
'diagnose_funnel' for backward compatibility with existing callers that pass
no action at all):

- 'diagnose_funnel' (default): computes real stage-to-stage conversion rates
  from funnel_events and flags the weakest transition as a candidate
  bottleneck. Deliberately does NOT recommend "send more outreach" as a
  response to a downstream bottleneck -- see docs/ARCHITECTURE.md Section 3:
  a stalled funnel gets diagnosed, not flooded.

- 'schedule_follow_ups': the real Prospecting -> Sales -> Follow-up ->
  Customer loop's follow-up step. Finds prospects stuck at 'outreach_sent'
  for longer than FOLLOW_UP_AFTER_DAYS with no reply, creates one real Sales
  task per prospect (a follow-up send, approval-gated exactly like the first
  send), and advances each to the 'follow_up' stage so it's never
  re-triggered twice for the same silence.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import TaskService
from packages.db.models import PROSPECT_STAGES, Agent, FunnelEvent, Prospect
from packages.db.scoped_session import scoped_session

MIN_SAMPLE_SIZE = 5  # below this, a "0% conversion" is noise, not a finding
FOLLOW_UP_AFTER_DAYS = 3


class GrowthAgent(BaseAgent):
    slug = "growth"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        context_refs = context_refs or {}
        action = context_refs.get("action", "diagnose_funnel")
        if action == "diagnose_funnel":
            return self._diagnose_funnel(ctx)
        if action == "schedule_follow_ups":
            return self._schedule_follow_ups(ctx, context_refs)
        raise ValueError(f"GrowthAgent received unknown action: {action!r}")

    def _diagnose_funnel(self, ctx: Context) -> dict:
        product_id = ctx.require_product()

        with scoped_session(ctx) as session:
            events = session.query(FunnelEvent).filter_by(product_id=product_id).all()
            reached = Counter(e.to_stage for e in events)

        transitions = []
        for i in range(len(PROSPECT_STAGES) - 1):
            from_stage, to_stage = PROSPECT_STAGES[i], PROSPECT_STAGES[i + 1]
            from_count = reached.get(from_stage, 0)
            to_count = reached.get(to_stage, 0)
            if from_count >= MIN_SAMPLE_SIZE:
                rate = round(to_count / from_count, 3) if from_count else 0.0
                transitions.append({"from": from_stage, "to": to_stage, "from_count": from_count, "to_count": to_count, "conversion_rate": rate})

        if not transitions:
            return {
                "status": "done",
                "finding": "insufficient_data",
                "note": f"No stage has reached the minimum sample size ({MIN_SAMPLE_SIZE}) yet.",
                "transitions": [],
            }

        weakest = min(transitions, key=lambda t: t["conversion_rate"])
        return {
            "status": "done",
            "finding": "bottleneck" if weakest["conversion_rate"] < 0.2 else "healthy",
            "weakest_transition": weakest,
            "transitions": transitions,
            "note": (
                "Diagnose before acting -- do not respond to a low conversion rate by "
                "increasing outreach volume (see docs/ARCHITECTURE.md Section 3)."
                if weakest["conversion_rate"] < 0.2 else "No transition below the 20% flag threshold."
            ),
        }

    def _schedule_follow_ups(self, ctx: Context, context_refs: Optional[dict] = None) -> dict:
        product_id = ctx.require_product()
        context_refs = context_refs or {}
        follow_up_days = int(context_refs.get("follow_up_after_days", FOLLOW_UP_AFTER_DAYS))
        cutoff = datetime.now(timezone.utc) - timedelta(days=follow_up_days)

        with scoped_session(ctx) as session:
            sales_agent_id = session.query(Agent).filter_by(slug="sales").one().id
            stale_prospects = session.query(Prospect).filter_by(product_id=product_id, stage="outreach_sent").all()

            candidates = []
            for prospect in stale_prospects:
                sent_event = (
                    session.query(FunnelEvent)
                    .filter_by(prospect_id=prospect.id, to_stage="outreach_sent")
                    .order_by(FunnelEvent.created_at.desc())
                    .first()
                )
                sent_at = sent_event.created_at if sent_event else None
                is_stale = sent_at is not None and (sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at) <= cutoff
                if is_stale:
                    candidates.append((prospect.id, prospect.company_name, prospect.contact_email))

            # Only people who were REALLY emailed get a follow-up (dry-run/logged sends never count).
            from packages.core import outbound
            really_contacted = outbound.real_first_contacts(ctx.company_id, [c[2] for c in candidates])
            candidates = [c for c in candidates if (c[2] or "").strip().lower() in really_contacted]

            for prospect_id, _, _ in candidates:
                prospect = session.get(Prospect, prospect_id)
                prospect.stage = "follow_up"
                session.add(FunnelEvent(prospect_id=prospect_id, product_id=product_id, from_stage="outreach_sent", to_stage="follow_up", agent="growth"))

        created_task_ids = []
        for prospect_id, company_name, _ in candidates:
            sub_ctx = Context(company_id=ctx.company_id, agent_id=sales_agent_id, product_id=product_id)
            refs = {"action": "draft_and_send", "prospect_id": str(prospect_id), "follow_up": True}
            if context_refs.get("origin"):
                refs["origin"] = context_refs["origin"]          # autopilot follow-ups obey the same window/pacing/cap
            task_id = TaskService.create(
                sub_ctx, goal=f"Follow up with {company_name} -- no reply after {follow_up_days} days",
                created_by="growth", context_refs=refs, priority=2,   # warmer than a cold first contact
            )
            created_task_ids.append(str(task_id))

        return {
            "status": "done",
            "follow_ups_scheduled": len(created_task_ids),
            "created_task_ids": created_task_ids,
        }
