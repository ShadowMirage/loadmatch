"""phase 11.2 performance indices

Revision ID: 9a1b2c3d4e5f
Revises: 73ab31c2777a
Create Date: 2026-03-24 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a1b2c3d4e5f'
down_revision = '73ab31c2777a'
branch_labels = None
depends_on = None


def upgrade():
    # 🚀 Add index for O(log n) reclaim detection
    op.create_index(
        'idx_execution_reclaim',
        'processed_messages',
        ['status', 'execution_started_at'],
        postgresql_where=sa.text("status = 'EXECUTING'")
    )


def downgrade():
    op.drop_index('idx_execution_reclaim')
