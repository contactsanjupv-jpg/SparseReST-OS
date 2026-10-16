"""row level security for product isolation

Revision ID: 5b803c32ed4b
Revises: f8967908eb88
Create Date: 2026-09-15 19:15:38.515628

Security model implemented here (see docs/SECURITY.md):

- Tables are owned by the migration/admin role (typically `postgres`).
  Postgres never applies RLS to a table's owner or to a superuser, so a
  second, least-privileged role (`app_role`) is created here, granted only
  the DML it needs, and it's the role the running application actually
  connects as (packages/db/scoped_session.py). RLS is meaningless if the app
  connects as the table owner; this migration makes sure it doesn't.

- Every product-scoped table gets a policy keyed on the Postgres session
  variables `app.current_product_id` (text: a product UUID, or the literal
  'NULL' for genuine company-level context) and `app.allow_cross_product`
  ('true' only when application code has already verified the calling agent
  is company-level -- see scoped_session()). This is defense in depth
  *underneath* that application check, not a replacement for it.

- Two different write policies, and this distinction matters:
    * DELEGATION/LOG tables (tasks, agent_runs, activity_events, audit_log):
      a company-level agent's actual job includes creating a task for a
      product-level agent on a specific product, tracking that agent's run,
      and logging its tool calls -- these are legitimate cross-product
      *writes* by design (that's what "CEO delegates to department agents"
      means concretely). WITH CHECK on these tables honors the
      allow_cross_product bypass.
    * EXECUTION-DATA tables (products, product_config, product_integrations,
      product_agent_enablement, prospects, funnel_events, experiments,
      knowledge_chunks): only the product-level agent actually enabled for
      that product should ever write these rows. WITH CHECK on these tables
      NEVER honors the bypass, even for a company-level agent -- a company-
      level agent may read rollups here, but never gets a write path into a
      specific product's execution data. This is a hard backstop, not a
      path anything in this codebase currently exercises.

  An earlier version of this migration denied cross-product writes on every
  table uniformly, which was wrong: it would have silently blocked the CEO
  agent from ever creating a task for a product-level agent. Caught by
  actually building the CEO agent's delegation path against it, not assumed
  correct from the design doc alone.

Coverage: products, product_config, product_integrations,
product_agent_enablement, tasks, agent_runs, activity_events, audit_log,
prospects, funnel_events, experiments, knowledge_chunks.

Not yet covered (documented, not silently assumed): companies, agents,
product_config_history, approvals, incidents -- either singleton registry
tables or tables that don't yet carry a direct product_id column. Tracked in
docs/ARCHITECTURE.md as a follow-up.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '5b803c32ed4b'
down_revision: Union[str, None] = 'f8967908eb88'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, product_id_nullable, id_is_own_product_id, cross_product_write_allowed)
RLS_TABLES = [
    ("products", False, True, False),                  # policy compares id (its own PK), not a product_id column
    ("product_config", False, False, False),
    ("product_integrations", False, False, False),
    ("product_agent_enablement", False, False, False),
    ("tasks", True, False, True),                       # delegation: CEO creates tasks for product-level agents
    ("agent_runs", True, False, True),                  # run tracking for a delegated task
    ("activity_events", True, False, True),             # activity logging follows the same delegation pattern
    ("audit_log", True, False, True),                   # tool-call logging follows the same delegation pattern
    ("prospects", False, False, False),
    ("funnel_events", False, False, False),
    ("experiments", False, False, False),
    ("knowledge_chunks", True, False, False),
]


def _policy_expressions(col: str, nullable: bool, table: str, cross_write_ok: bool):
    if nullable:
        using_expr = (
            f"(coalesce({col}::text, 'NULL') = current_setting('app.current_product_id', true) "
            f"OR current_setting('app.allow_cross_product', true) = 'true')"
        )
        strict_check = f"coalesce({col}::text, 'NULL') = current_setting('app.current_product_id', true)"
    elif table == "knowledge_chunks":
        using_expr = (
            f"({col} IS NULL OR {col}::text = current_setting('app.current_product_id', true) "
            f"OR current_setting('app.allow_cross_product', true) = 'true')"
        )
        strict_check = f"({col} IS NULL OR {col}::text = current_setting('app.current_product_id', true))"
    else:
        using_expr = (
            f"({col}::text = current_setting('app.current_product_id', true) "
            f"OR current_setting('app.allow_cross_product', true) = 'true')"
        )
        strict_check = f"{col}::text = current_setting('app.current_product_id', true)"

    if cross_write_ok:
        check_expr = f"({strict_check} OR current_setting('app.allow_cross_product', true) = 'true')"
    else:
        check_expr = f"({strict_check})"

    return using_expr, check_expr


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_role') THEN
                CREATE ROLE app_role LOGIN PASSWORD 'change_me_in_env';
            END IF;
        END
        $$;
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO app_role;")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_role;")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_role;")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_role;")

    for table, nullable, is_own_id, cross_write_ok in RLS_TABLES:
        col = "id" if is_own_id else "product_id"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        using_expr, check_expr = _policy_expressions(col, nullable, table, cross_write_ok)
        op.execute(f"""
            CREATE POLICY {table}_product_isolation ON {table}
            FOR ALL
            TO app_role
            USING {using_expr}
            WITH CHECK {check_expr};
        """)


def downgrade() -> None:
    for table, _, _, _ in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_product_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_role;")
