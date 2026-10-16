"""
Process-level plumbing for running the worker and inbound checker as long-lived,
supervised, horizontally-scalable processes:

* Heartbeats  -- every process records "I'm alive, and have done N tasks" in Postgres, so the
                 control plane (`/ops/workers`) can show which workers are actually running and
                 flag a dead one, instead of the founder discovering it from silence.
* Shutdown    -- SIGTERM/SIGINT set a flag; loops finish the task in hand and exit cleanly. This
                 is what makes `docker stop` / `systemctl restart` / rolling deploys safe: a task
                 is never abandoned half-executed just because the process was asked to stop.
* Singleton   -- a Postgres session-level advisory lock so exactly ONE process at a time does a
                 job that must not be duplicated (polling the mailbox, periodic maintenance),
                 while any number of task workers run in parallel. The lock lives on a dedicated
                 connection, so if the holder crashes Postgres releases it automatically and
                 another process takes over -- no stale lock files, no manual recovery.
"""
from __future__ import annotations

import os
import secrets
import signal
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import text

from packages.db.models import WorkerHeartbeat
from packages.db.session import get_engine, session_scope

# Arbitrary, stable 64-bit keys identifying each singleton job across all processes.
LOCK_INBOUND = 7_301_001
LOCK_MAINTENANCE = 7_301_002


def make_worker_id(role: str) -> str:
    return f"{role}-{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(2)}"


class GracefulShutdown:
    """Usage: `stop = GracefulShutdown().install()`, then loop on `while not stop.requested`
    and sleep with `stop.wait(seconds)` so a signal wakes the sleep immediately."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> "GracefulShutdown":
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._event.set())
        return self

    def request(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Sleeps up to `seconds`; returns True if shutdown was requested meanwhile."""
        return self._event.wait(seconds)


class HeartbeatService:
    @staticmethod
    def register(worker_id: str, role: str) -> None:
        with session_scope() as session:
            existing = session.get(WorkerHeartbeat, worker_id)
            if existing is None:
                session.add(WorkerHeartbeat(worker_id=worker_id, role=role, hostname=socket.gethostname(), pid=os.getpid(), status="running"))
            else:
                existing.status, existing.last_seen_at, existing.tasks_processed = "running", datetime.now(timezone.utc), 0

    @staticmethod
    def beat(worker_id: str, tasks_processed: int = 0, status: str = "running") -> None:
        with session_scope() as session:
            row = session.get(WorkerHeartbeat, worker_id)
            if row is not None:
                row.last_seen_at, row.tasks_processed, row.status = datetime.now(timezone.utc), tasks_processed, status

    @staticmethod
    def list_workers(stale_after_seconds: float = 60.0) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        with session_scope() as session:
            rows = session.query(WorkerHeartbeat).order_by(WorkerHeartbeat.role, WorkerHeartbeat.started_at).all()
            return [{
                "worker_id": r.worker_id, "role": r.role, "hostname": r.hostname, "pid": r.pid,
                "status": r.status, "tasks_processed": r.tasks_processed,
                "started_at": r.started_at.isoformat(), "last_seen_at": r.last_seen_at.isoformat(),
                # 'alive' means recently seen AND not cleanly stopped; a crashed process never wrote 'stopped'.
                "alive": r.status != "stopped" and r.last_seen_at >= cutoff,
            } for r in rows]

    @staticmethod
    def prune(older_than_days: int = 7) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        with session_scope() as session:
            return session.query(WorkerHeartbeat).filter(WorkerHeartbeat.last_seen_at < cutoff).delete()


class AdvisoryLock:
    """Non-blocking, session-level Postgres advisory lock held on its own connection."""

    def __init__(self, key: int):
        self.key = key
        self._conn = None

    def acquire(self) -> bool:
        if self._conn is not None:
            return True
        conn = get_engine().connect()
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}).scalar()
        conn.commit()
        if got:
            self._conn = conn
            return True
        conn.close()
        return False

    def release(self) -> None:
        if self._conn is not None:
            try:
                self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
                self._conn.commit()
            finally:
                self._conn.close()
                self._conn = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    def __enter__(self) -> "AdvisoryLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def workers_health(stale_after_seconds: float = 60.0) -> dict:
    workers = HeartbeatService.list_workers(stale_after_seconds)
    alive = [w for w in workers if w["alive"]]
    return {
        "workers_alive": sum(1 for w in alive if w["role"] == "worker"),
        "inbound_alive": sum(1 for w in alive if w["role"] == "inbound"),
        "workers": workers,
    }
