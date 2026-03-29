"""session_uniqueness_guard

Revision ID: d1f2e3a4b5c6
Revises: c4f2d1e8b9a0
Create Date: 2026-03-27 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d1f2e3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c4f2d1e8b9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM user_sessions AS stale
        USING (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY session_id
                        ORDER BY updated_at DESC NULLS LAST, id DESC
                    ) AS row_number
                FROM user_sessions
            ) ranked
            WHERE ranked.row_number > 1
        ) duplicates
        WHERE stale.id = duplicates.id
        """
    )
    op.create_unique_constraint(
        "uq_user_sessions_session_id",
        "user_sessions",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_user_sessions_session_id", "user_sessions", type_="unique")
