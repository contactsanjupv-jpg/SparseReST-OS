"""founder campaign goal on autopilot_state

Revision ID: c3e6a9b2d4f5
Revises: b2d5f8a1c3e4
Create Date: 2026-10-05 09:00:00.000000

The goal (customers + days) is supplied by the founder; there is no default, so autopilot cannot start without one.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c3e6a9b2d4f5'
down_revision: Union[str, None] = 'b2d5f8a1c3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('autopilot_state', sa.Column('campaign_status', sa.String(), nullable=False, server_default='unset'))
    op.add_column('autopilot_state', sa.Column('campaign_target', sa.Integer(), nullable=True))
    op.add_column('autopilot_state', sa.Column('campaign_days', sa.Integer(), nullable=True))
    op.add_column('autopilot_state', sa.Column('campaign_started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('autopilot_state', sa.Column('campaign_ended_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ('campaign_ended_at', 'campaign_started_at', 'campaign_days', 'campaign_target', 'campaign_status'):
        op.drop_column('autopilot_state', col)
