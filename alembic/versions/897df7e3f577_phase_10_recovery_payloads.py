"""phase_10_recovery_payloads

Revision ID: 897df7e3f577
Revises: 897df7e3f576
Create Date: 2026-03-22 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '897df7e3f577'
down_revision: Union[str, Sequence[str], None] = '897df7e3f576'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Add request_payload
    op.add_column('processed_messages', sa.Column('request_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    
    # 2. Alter response_payload type from JSON to JSONB
    op.execute('ALTER TABLE processed_messages ALTER COLUMN response_payload TYPE JSONB USING response_payload::text::jsonb')

def downgrade() -> None:
    op.execute('ALTER TABLE processed_messages ALTER COLUMN response_payload TYPE JSON USING response_payload::text::json')
    op.drop_column('processed_messages', 'request_payload')
