"""
Per docs/ARCHITECTURE.md, People/HR is explicitly assistive, not autonomous,
and hiring/firing is unconditionally on the approval-required list. This
agent's entire job in this codebase, honestly, is to never act on its own --
every request becomes a founder-facing approval/escalation. That's a real,
testable behavior, not a placeholder: the test is that it NEVER completes an
action without one.
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import ApprovalService, EventService


class PeopleHRAgent(BaseAgent):
    slug = "people_hr"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        approval_id = ApprovalService.request(
            task_id=ctx.task_id, requested_by=self.slug, risk_level="high",
            summary=f"People/HR request needs your review: {goal}",
        )
        EventService.emit(ctx, "people_hr.escalated", {"goal": goal, "approval_id": str(approval_id)})
        return {"status": "needs_approval", "approval_id": str(approval_id), "note": "People/HR requests are never actioned autonomously -- always escalated."}
