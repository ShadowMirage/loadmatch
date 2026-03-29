"""phase_10_distributed_workflow_runtime

Revision ID: 897df7e3f576
Revises: 8e4572f83b53
Create Date: 2026-03-21 16:56:09.660712

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '897df7e3f576'
down_revision: Union[str, Sequence[str], None] = '8e4572f83b53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('processed_messages', sa.Column('trace_id', sa.String(length=50), nullable=True))
    op.add_column('processed_messages', sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('processed_messages', sa.Column('error_log', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index('idx_retry_scan', 'processed_messages', ['status', 'retry_count'], unique=False)
    op.create_index(op.f('ix_processed_messages_trace_id'), 'processed_messages', ['trace_id'], unique=False)

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_processed_messages_trace_id'), table_name='processed_messages')
    op.drop_index('idx_retry_scan', table_name='processed_messages')
    op.drop_column('processed_messages', 'error_log')
    op.drop_column('processed_messages', 'retry_count')
    op.drop_column('processed_messages', 'trace_id')
