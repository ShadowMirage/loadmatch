"""production_hardening_v8

Revision ID: 8e4572f83b53
Revises: 42095dfa6eba
Create Date: 2026-03-21 13:47:23.236434

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8e4572f83b53'
down_revision: Union[str, Sequence[str], None] = '42095dfa6eba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Add production-grade O(1) indexes."""
    # 1. Atomic Idempotency Engine
    # Note: Check if unique index already exists from previous iterations
    op.create_unique_constraint('idx_idempotency_key', 'processed_messages', ['idempotency_key'])
    
    # 2. Lock Path Optimization
    op.create_index('idx_user_id', 'users', ['id'])
    op.create_index('idx_user_phone', 'users', ['phone'])
    
    # 3. TTL Scanning Optimization
    op.create_index('idx_user_updated_at', 'users', ['updated_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_user_updated_at', table_name='users')
    op.drop_index('idx_user_phone', table_name='users')
    op.drop_index('idx_user_id', table_name='users')
    op.drop_constraint('idx_idempotency_key', 'processed_messages', type_='unique')
