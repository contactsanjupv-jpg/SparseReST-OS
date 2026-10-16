"""The agent-runtime role (app_role) must not be able to touch credentials, even by accident or injection."""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from packages.db.session import get_app_engine


@pytest.mark.parametrize("table", ["secrets", "founder_users", "founder_sessions", "worker_heartbeats"])
@pytest.mark.parametrize("statement", ["SELECT * FROM {t}", "DELETE FROM {t}"])
def test_app_role_has_no_access_to_ops_tables(table, statement):
    with get_app_engine().connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text(statement.format(t=table)))


def test_app_role_still_has_access_to_product_tables():
    with get_app_engine().connect() as conn:
        conn.execute(text("SELECT 1 FROM agents LIMIT 1"))
