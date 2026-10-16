from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from packages.core.config import get_settings

_engine = None
_SessionLocal = None
_app_engine = None
_AppSessionLocal = None


def get_engine():
    """Admin/migration engine -- connects as the table owner (superuser in dev).
    RLS does not apply to this connection. Never use for agent/tool runtime queries."""
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, future=True)
    return _engine


def get_sessionmaker():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SessionLocal


def get_app_engine():
    """The RLS-enforced engine, connected as app_role. This is what
    packages/db/scoped_session.py uses -- every agent/tool query goes through it."""
    global _app_engine
    if _app_engine is None:
        _app_engine = create_engine(get_settings().app_database_url, future=True)
    return _app_engine


def get_app_sessionmaker():
    global _AppSessionLocal
    if _AppSessionLocal is None:
        _AppSessionLocal = sessionmaker(bind=get_app_engine(), autoflush=False, autocommit=False, future=True)
    return _AppSessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Admin transaction -- migrations, seeding, and test setup only."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def app_session_scope() -> Iterator[Session]:
    """RLS-enforced transaction, connected as app_role. Used exclusively by
    packages/db/scoped_session.py -- application/agent code should never call
    this directly without going through scoped_session(ctx)."""
    session = get_app_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
