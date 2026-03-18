"""system_improvements_dedup_ratelimit_expiry

Adds:
- processed_messages table (WhatsApp message deduplication)
- user_activity table (per-user rate limiting)
- expires_at column on load_requests
- expires_at column on truck_space_listings (with index)

Revision ID: a1b2c3d4e5f6
Revises: bea3a2fdc311
Create Date: 2026-03-16
"""
from typing import Union, Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'bea3a2fdc311'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── processed_messages ────────────────────────────────────────────────
    op.create_table(
        'processed_messages',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('wa_message_id', sa.String(128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=True),
    )
    op.create_unique_constraint(
        'uq_processed_messages_wa_message_id',
        'processed_messages',
        ['wa_message_id']
    )
    # Explicit index for O(1) lookup (also enforces uniqueness at DB level)
    op.create_index(
        'idx_wa_message_id',
        'processed_messages',
        ['wa_message_id'],
        unique=True
    )

    # ── user_activity ─────────────────────────────────────────────────────
    op.create_table(
        'user_activity',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=False, unique=True),
        sa.Column('message_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('window_start', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('idx_user_activity_user_id', 'user_activity', ['user_id'])

    # ── expires_at on load_requests ───────────────────────────────────────
    op.add_column(
        'load_requests',
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index('idx_load_requests_expires_at', 'load_requests', ['expires_at'])

    # ── expires_at on truck_space_listings ────────────────────────────────
    op.add_column(
        'truck_space_listings',
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index('idx_truck_listings_expires_at', 'truck_space_listings', ['expires_at'])


def downgrade() -> None:
    op.drop_index('idx_truck_listings_expires_at', table_name='truck_space_listings')
    op.drop_column('truck_space_listings', 'expires_at')

    op.drop_index('idx_load_requests_expires_at', table_name='load_requests')
    op.drop_column('load_requests', 'expires_at')

    op.drop_index('idx_user_activity_user_id', table_name='user_activity')
    op.drop_table('user_activity')

    op.drop_index('idx_wa_message_id', table_name='processed_messages')
    op.drop_constraint('uq_processed_messages_wa_message_id', 'processed_messages')
    op.drop_table('processed_messages')
