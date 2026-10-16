"""
Core domain services. Every write to tasks/activity_events/audit_log/approvals
goes through here, always via scoped_session(ctx) -- never a raw session.add()
scattered through agent code. This is what makes "every important action has
an audit trail" true by construction instead of by convention.
"""
from __future__ import annotations

import uuid
from typing import Optional

from packages.core.context import Context
from packages.db.models import (
    AGENT_RUN_STATUSES,
    Agent,
    AgentRun,
    Approval,
    Task,
)
from packages.db.scoped_session import scoped_session


class TaskService:
    @staticmethod
    def create(ctx: Context, *, goal: str, created_by: str, priority: int = 3,
               context_refs: Optional[dict] = None, workflow_id: Optional[uuid.UUID] = None) -> uuid.UUID:
        with scoped_session(ctx) as session:
            task = Task(
                id=uuid.uuid4(),
                company_id=ctx.company_id,
                product_id=ctx.product_id,
                agent_id=ctx.agent_id,
                workflow_id=workflow_id,
                created_by=created_by,
                goal=goal,
                context_refs=context_refs,
                priority=priority,
                status="pending",
            )
            session.add(task)
            session.flush()
            task_id = task.id
        EventService.emit(ctx.with_task(task_id, workflow_id), "task.created", {"goal": goal})
        return task_id

    @staticmethod
    def claim(ctx: Context, task_id: uuid.UUID) -> int:
        """Marks the task in_progress and increments its attempt counter -- the
        single place attempts is incremented, regardless of whether the task
        arrived here via the worker queue (packages/core/queue.py) or a direct
        .execute() call (tests, manual Playground 'run now'). Returns the new
        attempt count."""
        with scoped_session(ctx) as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not visible to this context (wrong product scope, or doesn't exist)")
            task.status = "in_progress"
            task.attempts = (task.attempts or 0) + 1
            attempts = task.attempts
        EventService.emit(ctx.with_task(task_id), "agent.working", {"attempt": attempts})
        return attempts

    @staticmethod
    def complete(ctx: Context, task_id: uuid.UUID, result: dict) -> None:
        with scoped_session(ctx) as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not visible to this context")
            task.status = "done"
            task.result = result
        EventService.emit(ctx.with_task(task_id), "agent.completed", {"result": result})

    @staticmethod
    def fail_or_retry(ctx: Context, task_id: uuid.UUID, reason: str) -> bool:
        """Returns True if the task failed TERMINALLY (attempts exhausted --
        caller should report an incident), False if a retry was scheduled
        instead. Never silently drops a failure into 'pending' forever: once
        max_attempts is reached, this always terminates the task."""
        from packages.core.queue import schedule_retry

        with scoped_session(ctx) as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not visible to this context")
            attempts, max_attempts = task.attempts, task.max_attempts

        if attempts < max_attempts:
            schedule_retry(task_id, attempts, reason)
            EventService.emit(ctx.with_task(task_id), "agent.retry_scheduled", {"attempt": attempts, "max_attempts": max_attempts, "reason": reason})
            return False

        with scoped_session(ctx) as session:
            task = session.get(Task, task_id)
            task.status = "failed"
            task.result = {"error": reason}
            task.last_error = reason
        EventService.emit(ctx.with_task(task_id), "agent.failed", {"reason": reason, "attempts": attempts})
        return True

    @staticmethod
    def defer(ctx: Context, task_id: uuid.UUID, until, reason: str) -> None:
        """Reschedule WITHOUT consuming a retry: the task did nothing wrong, it is just not allowed to run yet
        (outside the send window, pacing, daily cap...). attempts is rolled back because claim() counted this run."""
        with scoped_session(ctx) as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not visible to this context")
            task.status = "pending"
            task.next_attempt_at = until
            task.attempts = max(0, (task.attempts or 1) - 1)
            task.locked_by = None
            task.locked_at = None
            task.result = {"status": "deferred", "reason": reason, "retry_at": until.isoformat()}
        EventService.emit(ctx.with_task(task_id), "agent.deferred", {"reason": reason, "retry_at": until.isoformat()})

    @staticmethod
    def cancel(ctx: Context, task_id: uuid.UUID) -> None:
        with scoped_session(ctx) as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not visible to this context")
            if task.status in ("done", "failed", "cancelled"):
                raise ValueError(f"Task {task_id} is already terminal ({task.status}) -- nothing to cancel")
            task.status = "cancelled"
        EventService.emit(ctx.with_task(task_id), "agent.cancelled", {})

    @staticmethod
    def mark_needs_approval(ctx: Context, task_id: uuid.UUID, approval_id: uuid.UUID) -> None:
        with scoped_session(ctx) as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not visible to this context")
            task.status = "needs_approval"
            task.approval_id = approval_id
        EventService.emit(ctx.with_task(task_id), "agent.needs_approval", {"approval_id": str(approval_id)})


class AgentRunService:
    @staticmethod
    def start(ctx: Context, task_id: Optional[uuid.UUID], workflow_id: uuid.UUID) -> uuid.UUID:
        with scoped_session(ctx) as session:
            run = AgentRun(
                id=uuid.uuid4(), company_id=ctx.company_id, product_id=ctx.product_id,
                agent_id=ctx.agent_id, task_id=task_id, workflow_id=workflow_id, status="working",
            )
            session.add(run)
            session.flush()
            return run.id

    @staticmethod
    def set_status(ctx: Context, run_id: uuid.UUID, status: str, cost_usd: float = 0) -> None:
        if status not in AGENT_RUN_STATUSES:
            raise ValueError(f"Unknown agent_run status '{status}', expected one of {AGENT_RUN_STATUSES}")
        with scoped_session(ctx) as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise ValueError(f"AgentRun {run_id} not visible to this context")
            run.status = status
            run.cost_usd = (run.cost_usd or 0) + cost_usd
            if status in ("done", "error"):
                from datetime import datetime, timezone
                run.ended_at = datetime.now(timezone.utc)
        EventService.emit(ctx, f"agent.{status}", {"run_id": str(run_id)})


class EventService:
    @staticmethod
    def emit(ctx: Context, event_type: str, payload: Optional[dict] = None) -> None:
        from packages.db.models import ActivityEvent
        with scoped_session(ctx) as session:
            session.add(ActivityEvent(
                company_id=ctx.company_id, product_id=ctx.product_id, agent_id=ctx.agent_id,
                task_id=ctx.task_id, workflow_id=ctx.workflow_id, event_type=event_type, payload=payload,
            ))


class AuditService:
    @staticmethod
    def record(ctx: Context, *, tool: str, input: Optional[dict], output: Optional[dict],
               result: str, cost_usd: float = 0, approval_status: Optional[str] = None) -> None:
        from packages.db.models import AuditLog
        with scoped_session(ctx) as session:
            session.add(AuditLog(
                company_id=ctx.company_id, product_id=ctx.product_id, agent_id=ctx.agent_id,
                task_id=ctx.task_id, workflow_id=ctx.workflow_id, tool=tool,
                input=input, output=output, result=result, cost_usd=cost_usd, approval_status=approval_status,
            ))


class ApprovalService:
    """Approvals aren't yet product-scoped in the RLS sense (see the migration's
    docstring) -- this uses the admin session intentionally, not scoped_session,
    until `approvals` carries its own product_id. Tracked as a known gap, not
    silently pretended to be covered.

    decide() also touches the linked `tasks` row directly via the admin
    connection, which does bypass that table's own RLS -- deliberately: a
    founder's approve/reject decision is exactly the kind of trusted,
    already-authorized action the admin connection exists for elsewhere
    (migrations, seeding, the job queue's claim step), not a route for
    agent code to bypass isolation. No agent ever calls ApprovalService.decide;
    only the founder-facing API does.
    """

    @staticmethod
    def request(*, task_id: Optional[uuid.UUID], requested_by: str, summary: str, risk_level: str,
                details: Optional[dict] = None) -> uuid.UUID:
        """`details` is what the founder actually reviews (e.g. the full email). One PENDING approval per task:
        re-requesting while one is still pending returns the existing id instead of piling up duplicates."""
        from packages.db.session import session_scope
        with session_scope() as session:
            if task_id is not None:
                existing = session.query(Approval).filter_by(task_id=task_id, status="pending").first()
                if existing is not None:
                    if details is not None:
                        existing.details = details
                        existing.summary = summary
                    return existing.id
            approval = Approval(
                id=uuid.uuid4(), task_id=task_id, requested_by=requested_by,
                summary=summary, risk_level=risk_level, status="pending", details=details,
            )
            session.add(approval)
            session.flush()
            return approval.id

    @staticmethod
    def decide(approval_id: uuid.UUID, approved: bool) -> None:
        from datetime import datetime, timezone
        from packages.db.session import session_scope
        with session_scope() as session:
            approval = session.get(Approval, approval_id)
            if approval is None:
                raise ValueError(f"Approval {approval_id} not found")
            if approval.status != "pending":
                raise ValueError(f"Approval {approval_id} was already decided ({approval.status})")
            approval.status = "approved" if approved else "rejected"
            approval.decided_at = datetime.now(timezone.utc)
            task_id = approval.task_id

            if task_id is None:
                return  # nothing to resume -- this approval wasn't tied to a task

            task = session.get(Task, task_id)
            if task is None:
                return  # the task was deleted/doesn't exist -- nothing to resume

            if approved:
                new_refs = dict(task.context_refs or {})
                new_refs["approved"] = True
                task.context_refs = new_refs
                task.status = "pending"
                task.next_attempt_at = None
            else:
                task.status = "failed"
                task.last_error = "approval rejected by founder"
                task.result = {"error": "approval rejected by founder"}
                refs = task.context_refs or {}
                if refs.get("action") == "draft_and_send" and refs.get("prospect_id") and not refs.get("follow_up"):
                    # The founder looked at this draft and said no: that prospect is not contacted by this system.
                    # Marking it here (not just failing the task) is what stops the autopilot re-drafting it.
                    from packages.db.models import Prospect
                    prospect = session.get(Prospect, uuid.UUID(str(refs["prospect_id"])))
                    if prospect is not None and prospect.stage in ("qualified", "enriched"):
                        q = dict(prospect.qualification or {})
                        q["passed"], q["failed"] = False, list(q.get("failed", [])) + ["founder_rejected"]
                        prospect.qualification, prospect.stage = q, "icp_match"
