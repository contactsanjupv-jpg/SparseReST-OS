"""
The one place `SET LOCAL app.current_product_id` gets issued.

Every product-scoped DB operation goes through `scoped_session(context)` --
never `session_scope()` directly for agent/tool code. This is what turns the
RLS policies in the migration from theoretical into enforced: the session
variable they read is set here, from the caller's actual Context, inside the
same transaction as the query, so it can never be forgotten or spoofed by
code that doesn't go through this function.

`allow_cross_product` is never taken as a caller-supplied flag -- it's
derived from the agent's own `is_company_level` row in the `agents` table
inside this function, so an agent cannot request cross-product visibility for
itself just by asking. This is what backs the security model's promise that
even a bug in agent code can't leak across products at the DB layer.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from packages.core.context import Context
from packages.db.models import Agent
from packages.db.session import app_session_scope, session_scope


@contextmanager
def scoped_session(ctx: Context) -> Iterator[Session]:
    # Looking up the agent's own is_company_level flag is a cheap admin-side read --
    # the `agents` table isn't RLS-protected (it's a shared, non-product-scoped
    # registry), so this doesn't need to go through the app_role connection.
    with session_scope() as admin_session:
        agent = admin_session.get(Agent, ctx.agent_id)
        if agent is None:
            raise ValueError(f"Unknown agent_id {ctx.agent_id} -- refusing to open a scoped session for it")
        is_company_level = agent.is_company_level
        agent_slug = agent.slug

    if is_company_level:
        allow_cross_product = True
    else:
        if ctx.product_id is None:
            raise ValueError(
                f"Agent '{agent_slug}' is product-level but context has no product_id -- "
                "refusing to open a session with an ambiguous scope."
            )
        allow_cross_product = False

    product_id_setting = str(ctx.product_id) if ctx.product_id is not None else "NULL"

    # This is the connection RLS actually enforces against: app_role, never the
    # admin/superuser connection (Postgres never applies RLS to a table's owner
    # or a superuser, so using the admin connection here would silently disable
    # every isolation guarantee this module exists to provide).
    with app_session_scope() as session:
        # SET LOCAL does not accept bind parameters (verified: Postgres raises a
        # syntax error on `SET LOCAL x = $1`). set_config(..., is_local=true) is
        # the parameterized, injection-safe equivalent -- confirmed against an
        # injection-attempt value in testing.
        session.execute(
            text("SELECT set_config('app.current_product_id', :v, true)"),
            {"v": product_id_setting},
        )
        session.execute(
            text("SELECT set_config('app.allow_cross_product', :v, true)"),
            {"v": "true" if allow_cross_product else "false"},
        )
        yield session
