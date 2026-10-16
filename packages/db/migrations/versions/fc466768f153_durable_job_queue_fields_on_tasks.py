"""durable job queue fields on tasks

Revision ID: fc466768f153
Revises: 5b803c32ed4b
Create Date: 2026-09-17 09:08:59.714093
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'fc466768f153'
down_revision: Union[str, None] = '5b803c32ed4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('tasks', sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'))
    op.add_column('tasks', sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tasks', sa.Column('locked_by', sa.String(), nullable=True))
    op.add_column('tasks', sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tasks', sa.Column('last_error', sa.Text(), nullable=True))
    # Dequeue is `WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= now())
    # ORDER BY priority, created_at ... FOR UPDATE SKIP LOCKED` -- this index is what keeps that
    # query cheap once the tasks table has real volume, not just correct at dev-database size.
    op.create_index('ix_tasks_dequeue', 'tasks', ['status', 'priority', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_tasks_dequeue', table_name='tasks')
    op.drop_column('tasks', 'last_error')
    op.drop_column('tasks', 'locked_at')
    op.drop_column('tasks', 'locked_by')
    op.drop_column('tasks', 'next_attempt_at')
    op.drop_column('tasks', 'max_attempts')
    op.drop_column('tasks', 'attempts')
