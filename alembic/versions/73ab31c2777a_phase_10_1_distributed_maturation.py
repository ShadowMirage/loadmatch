"""phase_10_1_distributed_maturation

Revision ID: 73ab31c2777a
Revises: 897df7e3f577
Create Date: 2026-03-21 12:01:24.849827

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '73ab31c2777a'
down_revision: Union[str, Sequence[str], None] = '897df7e3f577'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add maturation columns to processed_messages
    op.add_column('processed_messages', sa.Column('execution_owner', sa.UUID(), nullable=True))
    op.add_column('processed_messages', sa.Column('recovery_attempted_at', sa.DateTime(), nullable=True))
    op.add_column('processed_messages', sa.Column('tenant_id', sa.UUID(), nullable=True))
    op.add_column('processed_messages', sa.Column('delivered_at', sa.DateTime(), nullable=True))
    op.add_column('processed_messages', sa.Column('execution_started_at', sa.DateTime(), nullable=True))

    # 2. Create workflow_events table for enterprise auditability
    op.create_table(
        'workflow_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('processed_message_id', sa.UUID(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('timestamp', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('payload', sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['processed_message_id'], ['processed_messages.id'], )
    )

    # 3. Performance Index for recovery scanning
    op.create_index(
        'idx_recovery_scan',
        'processed_messages',
        ['status', 'recovery_attempted_at', 'updated_at'],
        unique=False
    )


def downgrade() -> None:
    op.drop_index('idx_recovery_scan', table_name='processed_messages')
    op.drop_table('workflow_events')
    op.drop_column('processed_messages', 'execution_started_at')
    op.drop_column('processed_messages', 'delivered_at')
    op.drop_column('processed_messages', 'tenant_id')
    op.drop_column('processed_messages', 'recovery_attempted_at')
    op.drop_column('processed_messages', 'execution_owner')
