"""Heartbeats, graceful shutdown, singleton advisory locks, worker/inbound scripts, CLIs, preflight."""
from __future__ import annotations

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api.auth_service import AuthService
from apps.api.main import create_app
from packages.core.config import Settings, get_settings
from packages.core.inbound import process_inbound_for_company
from packages.core.workers import (
    LOCK_INBOUND, AdvisoryLock, GracefulShutdown, HeartbeatService, make_worker_id, workers_health,
)
from packages.db.models import Agent, Task, WorkerHeartbeat
from packages.db.session import session_scope
from packages.tools.imap_inbound import InboundReply
from scripts import check_production, create_founder, run_inbound, run_worker, secrets_cli
from tests.helpers import add_prospect, company_with_products


@pytest.fixture(autouse=True)
def _s():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _drop_worker(wid):
    with session_scope() as s:
        s.query(WorkerHeartbeat).filter_by(worker_id=wid).delete()


def test_heartbeat_lifecycle_and_staleness():
    wid = make_worker_id("worker")
    try:
        HeartbeatService.register(wid, "worker")
        HeartbeatService.beat(wid, 5)
        row = next(w for w in HeartbeatService.list_workers() if w["worker_id"] == wid)
        assert row["alive"] and row["tasks_processed"] == 5
        assert not next(w for w in HeartbeatService.list_workers(stale_after_seconds=-1) if w["worker_id"] == wid)["alive"]   # silent => dead
        HeartbeatService.beat(wid, 5, status="stopped")
        assert not next(w for w in HeartbeatService.list_workers() if w["worker_id"] == wid)["alive"]
        assert workers_health()["workers_alive"] >= 0
    finally:
        _drop_worker(wid)


def test_graceful_shutdown_wakes_sleep():
    stop = GracefulShutdown()
    threading.Timer(0.1, stop.request).start()
    t = time.monotonic()
    assert stop.wait(5) is True and time.monotonic() - t < 2 and stop.requested


def test_advisory_lock_is_exclusive_and_released_on_release():
    a, b = AdvisoryLock(LOCK_INBOUND + 99), AdvisoryLock(LOCK_INBOUND + 99)
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_advisory_lock_freed_when_holder_connection_dies():
    a = AdvisoryLock(LOCK_INBOUND + 98)
    assert a.acquire()
    a._conn.invalidate()                      # simulate a crashed process: connection gone, never released cleanly
    a._conn = None
    b = AdvisoryLock(LOCK_INBOUND + 98)
    deadline = time.time() + 5
    got = False
    while time.time() < deadline and not got:
        got = b.acquire()
        time.sleep(0.1)
    b.release()
    assert got


def test_worker_script_processes_queue_then_stops_and_marks_stopped():
    from packages.core.context import Context
    from packages.core.services import TaskService
    with company_with_products() as ids, session_scope() as s:
        agent_id = s.query(Agent).filter_by(slug="analytics").one().id
    with company_with_products() as ids:
        ctx = Context(company_id=ids["company_id"], agent_id=agent_id, product_id=None)
        task_id = TaskService.create(ctx, goal="rollup", created_by="test")
        before = {w["worker_id"] for w in HeartbeatService.list_workers()}
        assert run_worker.main(["--max-iterations", "3"], stop=GracefulShutdown()) == 0
        with session_scope() as s:
            assert s.get(Task, task_id).status in ("done", "pending", "failed")
            new = [w for w in HeartbeatService.list_workers() if w["worker_id"] not in before]
        assert new and all(w["status"] == "stopped" for w in new)
        for w in new:
            _drop_worker(w["worker_id"])
        with session_scope() as s:
            from packages.db.models import ActivityEvent, AgentRun
            s.query(ActivityEvent).filter_by(task_id=task_id).delete()
            s.query(AgentRun).filter_by(task_id=task_id).delete()
            from packages.db.models import AuditLog
            s.query(AuditLog).filter_by(task_id=task_id).delete()


def test_worker_stops_immediately_when_shutdown_already_requested():
    stop = GracefulShutdown(); stop.request()
    before = {w["worker_id"] for w in HeartbeatService.list_workers()}
    assert run_worker.main([], stop=stop) == 0
    for w in HeartbeatService.list_workers():
        if w["worker_id"] not in before:
            _drop_worker(w["worker_id"])


# ---- inbound: company-level, crash-safe -------------------------------------------------------------------

class FakeChecker:
    def __init__(self, replies, fail_mark=False):
        self.replies, self.marked, self.fail_mark = replies, [], fail_mark

    def fetch_unread_replies(self, mark_seen=True):
        assert mark_seen is False
        return self.replies

    def mark_seen(self, uids):
        if self.fail_mark:
            raise ConnectionError("imap down")
        self.marked += uids


def _reply(frm, uid="1", mid="<m1@x>", body="interested!"):
    return InboundReply(from_email=frm, subject="Re", body=body, imap_uid=uid, message_id=mid)


def test_inbound_routes_each_reply_to_the_right_product_and_marks_seen_after():
    with company_with_products(n_products=2) as ids:
        a = add_prospect(ids["company_id"], ids["product_ids"][0], name="A", email="a@a.test")
        b = add_prospect(ids["company_id"], ids["product_ids"][1], name="B", email="b@b.test")
        checker = FakeChecker([_reply("A@a.test", "1", "<1@x>"), _reply("b@b.test", "2", "<2@x>"), _reply("who@nowhere.test", "3", "<3@x>")])
        res = process_inbound_for_company(ids["company_id"], checker)
        assert len(res.task_ids) == 2 and res.unmatched == 1
        assert sorted(checker.marked) == ["1", "2", "3"]
        with session_scope() as s:
            by_product = {s.get(Task, t).product_id: s.get(Task, t).context_refs["prospect_id"] for t in res.task_ids}
        assert by_product == {ids["product_ids"][0]: str(a), ids["product_ids"][1]: str(b)}


def test_inbound_is_idempotent_on_message_id_when_mark_seen_fails():
    with company_with_products() as ids:
        add_prospect(ids["company_id"], ids["product_id"], name="A", email="a@a.test")
        checker = FakeChecker([_reply("a@a.test")], fail_mark=True)
        first = process_inbound_for_company(ids["company_id"], checker)      # crash window: task made, Seen not set
        second = process_inbound_for_company(ids["company_id"], checker)     # re-delivered
        assert len(first.task_ids) == 1 and second.duplicates == 1 and second.task_ids == []


def test_inbound_failure_to_route_leaves_message_unseen(monkeypatch):
    import packages.core.inbound as inb
    with company_with_products() as ids:
        monkeypatch.setattr(inb, "_route_reply", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db blip")))
        checker = FakeChecker([_reply("a@a.test")])
        res = process_inbound_for_company(ids["company_id"], checker)
        assert res.failed == 1 and checker.marked == []                      # retried next poll, never lost


def test_inbound_underscore_in_address_is_not_a_wildcard():
    with company_with_products() as ids:
        add_prospect(ids["company_id"], ids["product_id"], name="W", email="john_smith@x.test")
        res = process_inbound_for_company(ids["company_id"], FakeChecker([_reply("johnxsmith@x.test")]))
        assert res.unmatched == 1 and res.task_ids == []


def test_inbound_caps_reply_body_size():
    with company_with_products() as ids:
        add_prospect(ids["company_id"], ids["product_id"], name="A", email="a@a.test")
        res = process_inbound_for_company(ids["company_id"], FakeChecker([_reply("a@a.test", body="x" * 500_000)]))
        with session_scope() as s:
            assert len(s.get(Task, res.task_ids[0]).context_refs["reply_text"]) == 20_000


def test_inbound_script_exits_cleanly_when_imap_not_configured():
    assert run_inbound.main([], stop=GracefulShutdown()) == 2


def test_inbound_script_runs_one_cycle_with_injected_checker():
    with company_with_products():
        before = {w["worker_id"] for w in HeartbeatService.list_workers()}
        checker = FakeChecker([])
        stop = GracefulShutdown()
        import os
        os.environ["INBOUND_POLL_INTERVAL_SECONDS"] = "0.01"
        get_settings.cache_clear()
        try:
            assert run_inbound.main(["--max-iterations", "2"], stop=stop, checker=checker) == 0
        finally:
            os.environ.pop("INBOUND_POLL_INTERVAL_SECONDS", None)
        for w in HeartbeatService.list_workers():
            if w["worker_id"] not in before:
                assert w["status"] == "stopped"
                _drop_worker(w["worker_id"])


# ---- CLIs / preflight -----------------------------------------------------------------------------------------

def test_create_founder_cli_roundtrip_and_validation(capsys):
    email = f"cli-{uuid.uuid4().hex[:6]}@sparserest.test"
    pw = iter(["a-long-enough-passphrase", "a-long-enough-passphrase"])
    try:
        assert create_founder.main([email], password_reader=lambda _: next(pw)) == 0
        assert create_founder.main([email], password_reader=lambda _: "a-long-enough-passphrase") == 1      # duplicate
        assert create_founder.main([f"x{email}"], password_reader=lambda _: "short") == 1                    # weak
        mismatch = iter(["a-long-enough-passphrase", "different-long-passphrase"])
        assert create_founder.main([f"y{email}"], password_reader=lambda _: next(mismatch)) == 1
        assert create_founder.main([email, "--unlock"]) == 0
        pw2 = iter(["another-long-passphrase-9"] * 2)
        assert create_founder.main([email, "--reset-password"], password_reader=lambda _: next(pw2)) == 0
        AuthService.login(email, "another-long-passphrase-9")
    finally:
        from packages.db.models import FounderSession, FounderUser
        with session_scope() as s:
            ids = [u.id for u in s.query(FounderUser).filter(FounderUser.email.like("%cli-%")).all()]
            s.query(FounderSession).filter(FounderSession.user_id.in_(ids)).delete(synchronize_session=False)
            s.query(FounderUser).filter(FounderUser.id.in_(ids)).delete(synchronize_session=False)


def test_secrets_cli(capsys):
    from packages.core.secrets import generate_master_key
    settings = Settings(secrets_master_key=generate_master_key())
    name = f"cli{uuid.uuid4().hex[:6]}.k"
    try:
        assert secrets_cli.main(["set", name], value_reader=lambda _: "S3CRET-VALUE-zz", settings=settings) == 0
        assert secrets_cli.main(["list"], settings=settings) == 0
        out = capsys.readouterr().out
        assert name in out and "S3CRET-VALUE-zz" not in out                                # values are never printed
        assert secrets_cli.main(["rotate"], settings=settings) == 0
        assert secrets_cli.main(["delete", name], settings=settings) == 0
        assert secrets_cli.main(["list"], settings=Settings(secrets_master_key=None, secrets_master_key_file=None)) == 1
    finally:
        with session_scope() as s:
            from sqlalchemy import text
            s.execute(text("DELETE FROM secrets WHERE name=:n"), {"n": name})


def test_preflight_flags_unsafe_production_and_passes_a_good_one():
    bad = Settings(environment="production")
    errors, _ = check_production.audit(bad, accounts_exist=False, migrations_ok=False)
    assert len(errors) >= 5
    good = Settings(environment="production", allowed_origins="https://app.example.com", trusted_hosts="api.example.com",
                    app_database_url="postgresql+psycopg://app_role:xK9@db/x", secrets_master_key_file="/tmp/k")
    errors, _ = check_production.audit(good, accounts_exist=True, migrations_ok=True)
    assert errors == []
    assert any("32" in e for e in check_production.audit(Settings(environment="production", founder_api_key="short"), accounts_exist=True, migrations_ok=True)[0])


def test_ops_endpoints_report_workers_and_status():
    c = TestClient(create_app())
    st = c.get("/ops/status").json()
    assert st["schema_up_to_date"] is True and st["integrations"]["outreach"] == "stub"
    assert "password" not in str(st).lower()                        # integrations are reported as modes, never credentials
    assert "workers_alive" in c.get("/ops/workers").json()
    assert isinstance(c.post("/ops/maintenance/run").json(), dict)
