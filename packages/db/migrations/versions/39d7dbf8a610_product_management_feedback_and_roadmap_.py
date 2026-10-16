"""product management feedback and roadmap tables

Revision ID: 39d7dbf8a610
Revises: fc466768f153
Create Date: 2026-09-18 08:55:09.355798
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '39d7dbf8a610'
down_revision: Union[str, None] = 'fc466768f153'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('feedback',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('category', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('roadmap_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('feedback_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('product_id', 'title', name='uq_roadmap_items_product_title')
    )
    # NOTE: autogenerate proposed `op.drop_index('ix_tasks_dequeue', table_name='tasks')`
    # here -- deliberately removed. That index was created manually (a prior migration's
    # op.create_index call), not declared in the ORM model, so autogenerate always sees it
    # as "in the DB but not in metadata" and offers to drop it. It is real and load-bearing
    # for the job queue's dequeue query (see packages/core/queue.py) -- caught by actually
    # reading this generated migration before applying it, not assumed safe because Alembic
    # produced it. env.py now excludes this index from future autogenerate diffs so this
    # doesn't recur.


def downgrade() -> None:
    op.drop_table('roadmap_items')
    op.drop_table('feedback')
