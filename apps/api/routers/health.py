from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from packages.core.config import get_settings
from packages.db.session import get_engine

router = APIRouter()

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def _db_reachable() -> tuple[bool, str | None]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, str(exc)


def schema_state() -> dict:
    """Compares the database's applied migration to the code's newest one -- catches the
    classic 'deployed new code, forgot to migrate' failure before it becomes a 500."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_current_head()
    with get_engine().connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return {"current": current, "head": head, "up_to_date": current == head}


@router.get("/health")
def health():
    ok, detail = _db_reachable()
    if ok:
        return {"status": "ok", "database": "ok"}
    body = {"status": "error", "database": "unreachable"}
    if not get_settings().is_production:          # never echo driver errors (they can contain hostnames/users) to the internet
        body["detail"] = detail
    return JSONResponse(status_code=503, content=body)


@router.get("/health/live")
def live() -> dict:
    """Process is up. Deliberately touches nothing else -- a DB outage must not make an
    orchestrator kill and restart a healthy API process."""
    return {"status": "ok"}


@router.get("/health/ready")
def ready():
    ok, _ = _db_reachable()
    if not ok:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "database_unreachable"})
    try:
        state = schema_state()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "schema_check_failed"})
    if not state["up_to_date"]:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "migrations_pending", **state})
    return {"status": "ready", "schema": state}
