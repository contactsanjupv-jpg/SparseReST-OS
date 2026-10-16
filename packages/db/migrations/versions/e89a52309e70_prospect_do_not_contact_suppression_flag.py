"""prospect do not contact suppression flag

Revision ID: e89a52309e70
Revises: 131e3dde0f2c
Create Date: 2026-09-20 15:37:34.502640
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e89a52309e70'
down_revision: Union[str, None] = '131e3dde0f2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('prospects', sa.Column('do_not_contact', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('prospects', sa.Column('do_not_contact_reason', sa.String(), nullable=True))
    op.create_index('ix_prospects_do_not_contact', 'prospects', ['do_not_contact'])


def downgrade() -> None:
    op.drop_index('ix_prospects_do_not_contact', table_name='prospects')
    op.drop_column('prospects', 'do_not_contact_reason')
    op.drop_column('prospects', 'do_not_contact')
