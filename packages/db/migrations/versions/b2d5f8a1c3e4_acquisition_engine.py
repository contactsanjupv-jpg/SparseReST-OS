"""acquisition engine: autopilot state, send ledger, suppressions, growth reports, qualification/research columns

Revision ID: b2d5f8a1c3e4
Revises: a1c4e7b90d21
Create Date: 2026-10-02 09:00:00.000000

The four new tables are admin-connection-only: privileges are revoked from app_role so agent code
cannot flip its own safety switches, rewrite the send ledger that enforces the daily cap, or edit
the suppression list.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b2d5f8a1c3e4'
down_revision: Union[str, None] = 'a1c4e7b90d21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ADMIN_ONLY = ("autopilot_state", "outbound_messages", "suppressions", "growth_reports")
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB


def upgrade() -> None:
    op.add_column('prospects', sa.Column('qualification', JSONB, nullable=True))
    op.add_column('prospects', sa.Column('research', JSONB, nullable=True))
    op.add_column('prospects', sa.Column('reply_classification', sa.String(), nullable=True))
    op.add_column('prospects', sa.Column('replied_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('approvals', sa.Column('details', JSONB, nullable=True))
    op.add_column('incidents', sa.Column('dedup_key', sa.String(), nullable=True))
    op.add_column('incidents', sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_incidents_open_dedup', 'incidents', ['product_id', 'dedup_key'], postgresql_where=sa.text("status = 'open' AND dedup_key IS NOT NULL"))

    op.create_table(
        'autopilot_state',
        sa.Column('product_id', UUID, sa.ForeignKey('products.id'), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('mode', sa.String(), nullable=False, server_default='approval'),
        sa.Column('paused_reason', sa.Text(), nullable=True),
        sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('config', JSONB, nullable=False, server_default='{}'),
        sa.Column('cursor', JSONB, nullable=False, server_default='{}'),
        sa.Column('readiness', JSONB, nullable=True),
        sa.Column('updated_by', sa.String(), nullable=True),
        sa.Column('last_tick_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        'outbound_messages',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('company_id', UUID, nullable=False),
        sa.Column('product_id', UUID, nullable=True),
        sa.Column('prospect_id', UUID, nullable=True),
        sa.Column('task_id', UUID, nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('to_email', sa.String(), nullable=False),
        sa.Column('to_domain', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='reserved'),
        sa.Column('approval', sa.String(), nullable=False, server_default='founder'),
        sa.Column('provider_message_id', sa.String(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('bounce_type', sa.String(), nullable=True),
        sa.Column('reserved_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('bounced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_outbound_company_reserved', 'outbound_messages', ['company_id', 'reserved_at'])
    op.create_index('ix_outbound_to_email', 'outbound_messages', ['company_id', 'to_email'])
    op.create_index('ix_outbound_to_domain', 'outbound_messages', ['company_id', 'to_domain'])
    op.create_index('ix_outbound_prospect', 'outbound_messages', ['prospect_id'])
    op.create_table(
        'suppressions',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('company_id', UUID, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('value', sa.String(), nullable=False),
        sa.Column('reason', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False, server_default='system'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('company_id', 'kind', 'value', name='uq_suppression_company_kind_value'),
    )
    op.create_table(
        'growth_reports',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('company_id', UUID, nullable=False),
        sa.Column('product_id', UUID, sa.ForeignKey('products.id'), nullable=False),
        sa.Column('period', sa.String(), nullable=False),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metrics', JSONB, nullable=False),
        sa.Column('bottlenecks', JSONB, nullable=False, server_default='[]'),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('product_id', 'period', 'period_start', name='uq_growth_report_period'),
    )
    for table in ADMIN_ONLY:
        op.execute(f"REVOKE ALL ON {table} FROM app_role;")


def downgrade() -> None:
    for table in reversed(ADMIN_ONLY):
        op.drop_table(table)
    op.drop_index('ix_incidents_open_dedup', table_name='incidents')
    for table, col in (('incidents', 'escalated_at'), ('incidents', 'dedup_key'), ('approvals', 'details'),
                       ('prospects', 'replied_at'), ('prospects', 'reply_classification'),
                       ('prospects', 'research'), ('prospects', 'qualification')):
        op.drop_column(table, col)
