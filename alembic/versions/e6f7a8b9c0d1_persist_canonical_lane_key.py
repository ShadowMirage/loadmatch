"""persist_canonical_lane_key

Revision ID: e6f7a8b9c0d1
Revises: d1f2e3a4b5c6
Create Date: 2026-04-06 19:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import table, column

from app.services.logistics_data import normalize_hub_name


# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d1f2e3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


truck_space_listings = table(
    "truck_space_listings",
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


def upgrade() -> None:
    op.add_column(
        "truck_space_listings",
        sa.Column("canonical_lane_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_truck_space_listings_canonical_lane_key",
        "truck_space_listings",
        ["canonical_lane_key"],
        unique=False,
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            truck_space_listings.c.id,
            truck_space_listings.c.from_city,
            truck_space_listings.c.to_city,
        ).where(truck_space_listings.c.canonical_lane_key.is_(None))
    ).fetchall()

    for row in rows:
        lane_key = _canonical_lane_key(row.from_city, row.to_city)
        if not lane_key:
            continue
        bind.execute(
            truck_space_listings.update()
            .where(truck_space_listings.c.id == row.id)
            .values(canonical_lane_key=lane_key)
        )


def downgrade() -> None:
    op.drop_index("ix_truck_space_listings_canonical_lane_key", table_name="truck_space_listings")
    op.drop_column("truck_space_listings", "canonical_lane_key")
