"""row level security for feedback and roadmap items

Revision ID: 131e3dde0f2c
Revises: 39d7dbf8a610
Create Date: 2026-09-18 00:00:00.000000

Same pattern as the original RLS migration (5b803c32ed4b): execution-data
tables, no cross-product write bypass even for company-level agents. See
that migration's docstring for the full model.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '131e3dde0f2c'
down_revision: Union[str, None] = '39d7dbf8a610'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ["feedback", "roadmap_items"]


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY {table}_product_isolation ON {table}
            FOR ALL
            TO app_role
            USING (product_id::text = current_setting('app.current_product_id', true)
                   OR current_setting('app.allow_cross_product', true) = 'true')
            WITH CHECK (product_id::text = current_setting('app.current_product_id', true));
        """)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_product_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
