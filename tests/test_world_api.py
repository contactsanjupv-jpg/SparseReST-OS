"""Company World feed: state is derived from real rows only; nothing is invented."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.routers.world import ACTIVE_WINDOW, derive_agent_state
from packages.db.models import ActivityEvent, Agent, AgentRun, Task
from packages.db.session import session_scope
from tests.helpers import company_with_products

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def run(status, started_ago_min=1, ended=False):
    return AgentRun(status=status, started_at=NOW - timedelta(minutes=started_ago_min), ended_at=NOW if ended else None)


def test_state_derivation_rules():
    assert derive_agent_state(None, False, NOW)[0] == "idle"
    assert derive_agent_state(run("working"), False, NOW)[0] == "working"
    assert derive_agent_state(run("working", started_ago_min=ACTIVE_WINDOW.seconds // 60 + 5), False, NOW)[0] == "stalled"   # silent run != busy
    assert derive_agent_state(run("error", ended=True), False, NOW)[0] == "error"
    assert derive_agent_state(run("error", started_ago_min=500), False, NOW)[0] == "idle"          # old failures fade
    assert derive_agent_state(run("done", ended=True), False, NOW)[0] == "idle"
    assert derive_agent_state(run("working"), True, NOW)[0] == "needs_approval"                     # waiting on founder wins


def test_world_state_reflects_real_run_and_events():
    with company_with_products() as ids:
        with session_scope() as s:
            agent = s.query(Agent).filter_by(slug="sales").one()
            task = Task(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=agent.id, created_by="test", goal="world-test goal", status="in_progress")
            s.add(task); s.flush()
            s.add(AgentRun(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=agent.id, task_id=task.id, workflow_id=uuid.uuid4(), status="working"))
            s.add(ActivityEvent(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=agent.id, task_id=task.id,
                                event_type="tool.call", payload={"smtp_password": "hunter2", "note": "x" * 1000}))
            s.flush()
            task_id = task.id
        c = TestClient(create_app())
        state = c.get(f"/world/state?product_id={ids['product_id']}").json()
        # product-scoped view only lists company-level agents + those enabled for the product (none enabled here)
        assert all(a["is_company_level"] for a in state["agents"])
        full = c.get("/world/state").json()
        sales = next(a for a in full["agents"] if a["slug"] == "sales")
        assert sales["id"] and sales["state"] in ("working", "stalled", "idle", "needs_approval", "error")

        ev = c.get("/world/events?after_id=0&limit=200").json()
        mine = [e for e in ev["events"] if e["task_id"] == str(task_id)]
        assert mine and mine[0]["payload"]["smtp_password"] == "[REDACTED]" and len(mine[0]["payload"]["note"]) < 300
        assert c.get(f"/world/events?after_id={ev['cursor']}").json()["events"] == []             # cursor makes polling incremental
        with session_scope() as s:
            s.query(ActivityEvent).filter_by(task_id=task_id).delete()
            s.query(AgentRun).filter_by(task_id=task_id).delete()
            s.query(Task).filter_by(id=task_id).delete()


def test_world_requires_auth_when_configured(monkeypatch):
    from packages.core.config import get_settings
    monkeypatch.setenv("FOUNDER_API_KEY", "k" * 40); get_settings.cache_clear()
    c = TestClient(create_app())
    assert c.get("/world/state").status_code == 401
    assert c.get("/world/state", headers={"Authorization": "Bearer " + "k" * 40}).status_code == 200
    get_settings.cache_clear()
