"""add_load_request_vehicle_type

Revision ID: a2b3c4d5e6f7
Revises: f2a3b4c5d6e7
Create Date: 2026-04-08 00:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "load_requests",
        sa.Column("vehicle_type", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_load_requests_vehicle_type",
        "load_requests",
        ["vehicle_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_load_requests_vehicle_type", table_name="load_requests")
    op.drop_column("load_requests", "vehicle_type")
