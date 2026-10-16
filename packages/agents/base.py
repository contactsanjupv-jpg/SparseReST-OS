"""
Every department agent extends BaseAgent and implements `handle()` only --
AgentRun creation, status transitions (working -> done/error), retry-vs-
terminal-failure decisions, and the corresponding activity_events/incidents
are handled once, here, so no agent implementation can forget to report its
own state honestly. This is the concrete answer to "if an agent fails, the
system needs to know it failed": failure is caught here and always recorded
-- retried if attempts remain, escalated to a critical Incident if not --
never swallowed.
"""
from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from packages.core.context import Context
from packages.core.incidents import IncidentService
from packages.core.log import bind_log_context, get_logger, log
from packages.core.services import AgentRunService, TaskService

_log = get_logger("agent")


class AgentExecutionError(Exception):
    """Raised (after being recorded) when an agent's handle() raises. Callers
    of execute() see this, so upstream retry/escalation logic can act on it --
    the failure is never just logged and silently absorbed."""


class BaseAgent(ABC):
    slug: str

    @abstractmethod
    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        """Do the actual work. Return a JSON-serializable result dict.
        Raise on failure -- do not return a 'fake success' result to avoid
        an error path; execute() records failure state correctly."""
        raise NotImplementedError

    def execute(self, ctx: Context, task_id: uuid.UUID, *, goal: str, context_refs: Optional[dict] = None) -> dict:
        workflow_id = ctx.workflow_id or uuid.uuid4()
        ctx = ctx.with_task(task_id, workflow_id)
        # Every log line from here down -- including from tools and services this agent calls --
        # carries the full context, so any line can be joined back to its task/audit rows.
        with bind_log_context(company_id=ctx.company_id, product_id=ctx.product_id, agent_id=ctx.agent_id,
                              agent=self.slug, task_id=task_id, workflow_id=workflow_id):
            return self._execute_bound(ctx, task_id, goal=goal, context_refs=context_refs)

    def _execute_bound(self, ctx: Context, task_id: uuid.UUID, *, goal: str, context_refs: Optional[dict]) -> dict:
        started = time.perf_counter()
        TaskService.claim(ctx, task_id)
        run_id = AgentRunService.start(ctx, task_id, workflow_id=ctx.workflow_id)
        log(_log, logging.INFO, "agent task started", goal=goal)

        try:
            result = self.handle(ctx, goal=goal, context_refs=context_refs)
        except Exception as exc:
            AgentRunService.set_status(ctx, run_id, "error")
            terminal = TaskService.fail_or_retry(ctx, task_id, reason=str(exc))
            if terminal:
                refs = context_refs or {}
                # De-duplicated: while an incident for this same failing action is open, a repeat failure does not
                # raise (or notify) another one -- one outage is one actionable incident, not a stream of pages.
                incident_key = refs.get("incident_key") or f"agent_failed:{self.slug}:{refs.get('action') or goal[:60]}"
                IncidentService().report(
                    company_id=ctx.company_id, product_id=ctx.product_id, agent_id=ctx.agent_id, task_id=task_id,
                    severity=refs.get("incident_severity", "critical"), dedup_key=incident_key,
                    title=f"'{self.slug}' agent failed permanently on: {goal}",
                    cause=str(exc),
                    impact=f"Task did not complete after exhausting retries -- goal '{goal}' was not achieved.",
                    action_required="Review the error and the underlying tool/integration, then retry the task once fixed.",
                )
            raise AgentExecutionError(f"{self.slug} failed on task {task_id}: {exc}") from exc

        AgentRunService.set_status(ctx, run_id, "done")

        if isinstance(result, dict) and result.get("status") == "deferred" and result.get("retry_at"):
            # Not a failure and not done: the agent is not ALLOWED to act yet (send window, pacing, daily cap,
            # safety pause). Reschedule without burning a retry; the worker picks it up at retry_at.
            from datetime import datetime
            TaskService.defer(ctx, task_id, datetime.fromisoformat(result["retry_at"]), result.get("reason", "deferred"))
        elif isinstance(result, dict) and result.get("status") == "needs_approval" and result.get("approval_id"):
            # The task itself is NOT done -- it's paused pending a founder decision.
            # Marking it 'done' here (the old behavior) would make ApprovalService's
            # approve-and-resume flow (packages/core/services.py) impossible, since a
            # 'done' task looks finished to the queue and nothing would ever pick it
            # back up after approval.
            TaskService.mark_needs_approval(ctx, task_id, approval_id=uuid.UUID(result["approval_id"]))
        else:
            TaskService.complete(ctx, task_id, result=result)
        log(_log, logging.INFO, "agent task finished", duration_ms=round((time.perf_counter() - started) * 1000, 1),
            outcome=(result.get("status") if isinstance(result, dict) else None))
        return result
