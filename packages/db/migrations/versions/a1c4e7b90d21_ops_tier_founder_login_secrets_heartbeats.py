"""ops tier: founder login, encrypted secrets store, worker heartbeats

Revision ID: a1c4e7b90d21
Revises: e89a52309e70
Create Date: 2026-09-23 09:00:00.000000

These tables hold credentials and liveness data, not product data, so they are
admin-connection-only: every privilege is REVOKED from app_role (which gets
blanket table privileges via ALTER DEFAULT PRIVILEGES in the RLS migration).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1c4e7b90d21'
down_revision: Union[str, None] = 'e89a52309e70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OPS_TABLES = ("founder_users", "founder_sessions", "secrets", "worker_heartbeats")


def upgrade() -> None:
    op.create_table(
        'founder_users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('email', sa.String(), nullable=False, unique=True),
        sa.Column('password_hash', sa.String(), nullable=False),
        sa.Column('totp_secret_enc', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('failed_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('password_changed_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        'founder_sessions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('founder_users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip', sa.String(), nullable=True),
        sa.Column('user_agent', sa.String(), nullable=True),
    )
    op.create_index('ix_founder_sessions_user_id', 'founder_sessions', ['user_id'])
    op.create_index('ix_founder_sessions_expires_at', 'founder_sessions', ['expires_at'])
    op.create_table(
        'secrets',
        sa.Column('name', sa.String(), primary_key=True),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        'worker_heartbeats',
        sa.Column('worker_id', sa.String(), primary_key=True),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('hostname', sa.String(), nullable=False),
        sa.Column('pid', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='running'),
        sa.Column('tasks_processed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Credentials never reachable by the agent-runtime role, even by accident.
    for table in OPS_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM app_role;")


def downgrade() -> None:
    op.drop_table('worker_heartbeats')
    op.drop_table('secrets')
    op.drop_index('ix_founder_sessions_expires_at', table_name='founder_sessions')
    op.drop_index('ix_founder_sessions_user_id', table_name='founder_sessions')
    op.drop_table('founder_sessions')
    op.drop_table('founder_users')
