"""
Polling-based SSE stream of a task's real activity_events -- consistent with
the $0-cost-first, Postgres-only infrastructure choice everywhere else in
this system (no Redis pub/sub, no message broker). Polling every
`poll_interval_seconds` against an indexed query is cheap at this system's
actual scale; revisit only if a measured need shows up.

This never sends anything that isn't a real row from activity_events -- no
synthetic "still working..." heartbeat content, no fabricated progress
percentage. A real heartbeat comment (a bare ": keep-alive\n\n") is fine to
keep some proxies from timing out the connection, since it's explicitly not
data.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Iterator, Optional

from packages.db.models import ActivityEvent, Task
from packages.db.session import session_scope

TERMINAL_STATUSES = ("done", "failed", "cancelled")


def stream_task_events(
    task_id: uuid.UUID,
    *,
    poll_interval_seconds: float = 0.5,
    max_seconds: float = 30.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> Iterator[str]:
    """Yields SSE-formatted strings ('data: {...}\\n\\n'). Stops when the task
    reaches a terminal status (done/failed/cancelled) -- yielding one final
    event announcing that -- or when max_seconds elapses, whichever first.
    sleep_fn/clock_fn are injectable so this is testable without a real sleep."""
    last_seen_id = 0
    started_at = clock_fn()

    while True:
        with session_scope() as session:
            new_events = (
                session.query(ActivityEvent)
                .filter(ActivityEvent.task_id == task_id, ActivityEvent.id > last_seen_id)
                .order_by(ActivityEvent.id.asc())
                .all()
            )
            for event in new_events:
                last_seen_id = event.id
                yield _format_sse({
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                })

            task = session.get(Task, task_id)
            task_status = task.status if task else None

        if task_status in TERMINAL_STATUSES:
            yield _format_sse({"event_type": "stream.closed", "payload": {"final_status": task_status}})
            return

        if clock_fn() - started_at >= max_seconds:
            yield _format_sse({"event_type": "stream.timeout", "payload": {"last_known_status": task_status}})
            return

        sleep_fn(poll_interval_seconds)


def _format_sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
