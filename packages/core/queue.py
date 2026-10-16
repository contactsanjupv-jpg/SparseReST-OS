"""
Tasks ARE the job queue -- no separate jobs table, no external broker. This
uses Postgres's `FOR UPDATE SKIP LOCKED` pattern, which is what makes
concurrent-worker-safe dequeue possible without Redis or a message broker:
two workers racing to claim the same row will never both succeed, because
the row lock makes the second worker's query skip past it and take the next
one instead. This is a well-established pattern, not a novel one -- chosen
specifically because it needs zero infrastructure beyond the Postgres this
system already requires (see docs/ARCHITECTURE.md's $0-cost-first principle).

This module intentionally uses the ADMIN connection (session_scope), not
scoped_session: claiming a job is a trusted queue-infrastructure operation
that must be able to see pending work across every product (a worker
draining the whole company's queue), not an agent acting on a specific
product's data. Once a task is claimed, the actual agent execution
(packages/agents/base.py::BaseAgent.execute) goes through scoped_session as
normal -- this boundary is deliberate, not a bypass of product isolation for
the work itself.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from packages.db.session import session_scope

BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 300
STUCK_AFTER_SECONDS = 300  # a lock held this long with no completion is presumed dead


def backoff_seconds(attempts: int) -> int:
    """Exponential backoff, capped -- attempt 1 waits 5s, attempt 2 waits 10s,
    attempt 3 waits 20s, etc., never exceeding BACKOFF_CAP_SECONDS."""
    return min(BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_SECONDS)


class ClaimedTask:
    def __init__(self, id, company_id, product_id, agent_id, goal, context_refs, attempts, max_attempts):
        self.id, self.company_id, self.product_id, self.agent_id = id, company_id, product_id, agent_id
        self.goal, self.context_refs, self.attempts, self.max_attempts = goal, context_refs, attempts, max_attempts


def claim_next_task(worker_id: str, product_id: Optional[uuid.UUID] = None) -> Optional[ClaimedTask]:
    """Atomically claims and locks the next eligible pending task, or returns
    None if the queue (for this scope) is empty. Safe to call concurrently
    from multiple workers -- SKIP LOCKED guarantees no two workers ever claim
    the same row."""
    with session_scope() as session:
        product_filter = "AND product_id = :product_id" if product_id is not None else ""
        params = {"worker_id": worker_id, "now": datetime.now(timezone.utc)}
        if product_id is not None:
            params["product_id"] = product_id

        row = session.execute(text(f"""
            UPDATE tasks
            SET status = 'in_progress', locked_by = :worker_id, locked_at = :now, updated_at = :now
            WHERE id = (
                SELECT id FROM tasks
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
                  {product_filter}
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, company_id, product_id, agent_id, goal, context_refs, attempts, max_attempts
        """), params).mappings().one_or_none()

        if row is None:
            return None
        return ClaimedTask(
            id=row["id"], company_id=row["company_id"], product_id=row["product_id"],
            agent_id=row["agent_id"], goal=row["goal"], context_refs=row["context_refs"],
            attempts=row["attempts"], max_attempts=row["max_attempts"],
        )


def schedule_retry(task_id: uuid.UUID, attempts: int, error: str) -> None:
    with session_scope() as session:
        session.execute(text("""
            UPDATE tasks
            SET status = 'pending', next_attempt_at = :next_attempt_at,
                last_error = :error, locked_by = NULL, locked_at = NULL
            WHERE id = :task_id
        """), {
            "task_id": task_id,
            "next_attempt_at": datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds(attempts)),
            "error": error,
        })


def requeue_stuck_tasks(stuck_after_seconds: int = STUCK_AFTER_SECONDS) -> list[uuid.UUID]:
    """Reaper: finds tasks locked 'in_progress' for longer than the threshold
    with no resolution (the worker that claimed them presumably crashed) and
    puts them back in the queue for another attempt, respecting the same
    attempts/max_attempts budget as a normal failure. Returns the IDs it
    requeued or terminally failed, so a caller can log/alert on them."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stuck_after_seconds)
    with session_scope() as session:
        rows = session.execute(text("""
            SELECT id, attempts, max_attempts FROM tasks
            WHERE status = 'in_progress' AND locked_at IS NOT NULL AND locked_at < :cutoff
        """), {"cutoff": cutoff}).mappings().all()

    affected = []
    for row in rows:
        if row["attempts"] >= row["max_attempts"]:
            with session_scope() as session:
                session.execute(text("""
                    UPDATE tasks SET status = 'failed', last_error = 'stuck: worker lock expired and attempts exhausted',
                    locked_by = NULL, locked_at = NULL WHERE id = :id
                """), {"id": row["id"]})
        else:
            schedule_retry(row["id"], row["attempts"], "stuck: worker lock expired, presumed crashed")
        affected.append(row["id"])
    return affected
