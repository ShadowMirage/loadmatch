"""persist_load_request_lane_key

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-08 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import column, table

from app.services.logistics_data import normalize_hub_name


# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


load_requests = table(
    "load_requests",
    column("id", sa.String),
    column("from_city", sa.String),
    column("to_city", sa.String),
    column("canonical_lane_key", sa.String),
)


def _canonical_lane_key(from_city: str | None, to_city: str | None) -> str | None:
    normalized_from = normalize_hub_name(from_city or "")
    normalized_to = normalize_hub_name(to_city or "")
    if not normalized_from or not normalized_to:
        return None
    ordered = sorted([normalized_from, normalized_to])
    return f"{ordered[0]}:{ordered[1]}"


def _backfill_load_request_lane_keys(bind) -> dict[str, int]:
    rows = bind.execute(
        sa.select(
            load_requests.c.id,
            load_requests.c.from_city,
            load_requests.c.to_city,
        ).where(load_requests.c.canonical_lane_key.is_(None))
    ).fetchall()

    updated_rows = 0
    for row in rows:
        lane_key = _canonical_lane_key(row.from_city, row.to_city)
        if not lane_key:
            continue
        bind.execute(
            load_requests.update()
            .where(load_requests.c.id == row.id)
            .values(canonical_lane_key=lane_key)
        )
        updated_rows += 1

    return {"scanned_rows": len(rows), "updated_rows": updated_rows}


def upgrade() -> None:
    op.add_column(
        "load_requests",
        sa.Column("canonical_lane_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_load_requests_canonical_lane_key",
        "load_requests",
        ["canonical_lane_key"],
        unique=False,
    )
    _backfill_load_request_lane_keys(op.get_bind())


def downgrade() -> None:
    op.drop_index("ix_load_requests_canonical_lane_key", table_name="load_requests")
    op.drop_column("load_requests", "canonical_lane_key")
