"""
route_subscriptions table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-16
"""
from typing import Union, Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'route_subscriptions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('pickup_city',      sa.String(100), nullable=False),
        sa.Column('drop_city',        sa.String(100), nullable=False),
        sa.Column('normalized_pickup', sa.String(100), nullable=False),
        sa.Column('normalized_drop',   sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=True),
    )
    # Composite index for fast corridor lookups
    op.create_index(
        'idx_route_sub_corridor',
        'route_subscriptions',
        ['normalized_pickup', 'normalized_drop']
    )
    op.create_index(
        'idx_route_sub_user',
        'route_subscriptions',
        ['user_id']
    )


def downgrade() -> None:
    op.drop_index('idx_route_sub_user',     table_name='route_subscriptions')
    op.drop_index('idx_route_sub_corridor', table_name='route_subscriptions')
    op.drop_table('route_subscriptions')
