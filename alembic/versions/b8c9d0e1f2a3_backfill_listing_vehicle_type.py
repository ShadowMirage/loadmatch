"""backfill_listing_vehicle_type

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-04-08 12:05:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE truck_space_listings tsl
        SET vehicle_type = CAST(t.truck_type AS VARCHAR(32))
        FROM trucks t
        WHERE tsl.truck_id = t.id
        AND tsl.vehicle_type IS NULL
        """
    )

    bind = op.get_bind()
    missing = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM truck_space_listings
            WHERE status IN ('open','partial')
            AND vehicle_type IS NULL
            """
        )
    ).scalar()
    if int(missing or 0) > 0:
        raise RuntimeError(
            "vehicle_type backfill incomplete for open/partial listings; aborting rollout"
        )


def downgrade() -> None:
    op.execute("UPDATE truck_space_listings SET vehicle_type = NULL")
