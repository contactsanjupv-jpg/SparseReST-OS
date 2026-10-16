"""
Auth: verifies the founder-key dependency actually rejects a missing/wrong
key when configured, and stays open when unconfigured (documented behavior,
not silent).

Streaming: drives the real generator (apps/api/streaming.py) against real
activity_events rows, with sleep/clock injected so the test doesn't actually
wait -- proves events are yielded in order and the stream terminates on a
terminal task status, not left hanging forever.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from apps.api.auth import require_founder_key
from apps.api.streaming import stream_task_events
from packages.core.config import get_settings
from packages.db.models import ActivityEvent, Agent, Company, Task
from packages.db.session import session_scope


def test_auth_open_when_no_key_configured():
    get_settings.cache_clear()
    assert get_settings().founder_api_key is None
    require_founder_key(authorization=None)  # must not raise


def test_auth_rejects_missing_or_wrong_key(monkeypatch):
    monkeypatch.setenv("FOUNDER_API_KEY", "secret123")
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as exc_info:
            require_founder_key(authorization=None)
        assert exc_info.value.status_code == 401

        with pytest.raises(HTTPException):
            require_founder_key(authorization="Bearer wrong")

        require_founder_key(authorization="Bearer secret123")  # must not raise
    finally:
        monkeypatch.delenv("FOUNDER_API_KEY", raising=False)
        get_settings.cache_clear()


@pytest.fixture()
def streaming_task():
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"stream-test-{uuid.uuid4().hex[:8]}", name="Stream Test")
        session.add(company)
        session.flush()
        agent = session.query(Agent).filter_by(slug="ceo").one()
        task = Task(company_id=company.id, product_id=None, agent_id=agent.id, created_by="test", goal="stream test", status="in_progress")
        session.add(task)
        session.flush()
        ids = {"company_id": company.id, "task_id": task.id}
        session.commit()

    yield ids

    with session_scope() as session:
        session.query(ActivityEvent).filter_by(task_id=ids["task_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(id=ids["task_id"]).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_stream_yields_events_in_order_then_closes_on_terminal_status(streaming_task):
    with session_scope() as session:
        for i, event_type in enumerate(["agent.working", "tool_called", "agent.completed"]):
            session.add(ActivityEvent(company_id=streaming_task["company_id"], task_id=streaming_task["task_id"], event_type=event_type, payload={"i": i}))
        session.get(Task, streaming_task["task_id"]).status = "done"

    fake_clock = {"t": 0.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        fake_clock["t"] += seconds

    events = list(stream_task_events(
        streaming_task["task_id"], sleep_fn=fake_sleep, clock_fn=lambda: fake_clock["t"],
    ))

    event_types = [_extract_event_type(e) for e in events]
    assert event_types == ["agent.working", "tool_called", "agent.completed", "stream.closed"]
    assert sleeps == [], "task was already terminal on the first poll -- must not have slept waiting for more"


def test_stream_times_out_if_task_never_completes(streaming_task):
    fake_clock = {"t": 0.0}

    def fake_sleep(seconds):
        fake_clock["t"] += 100  # jump straight past max_seconds so the test doesn't loop 60 times

    events = list(stream_task_events(
        streaming_task["task_id"], max_seconds=5.0, sleep_fn=fake_sleep, clock_fn=lambda: fake_clock["t"],
    ))
    assert _extract_event_type(events[-1]) == "stream.timeout"


def _extract_event_type(sse_line: str) -> str:
    import json
    assert sse_line.startswith("data: ")
    return json.loads(sse_line[len("data: "):])["event_type"]
