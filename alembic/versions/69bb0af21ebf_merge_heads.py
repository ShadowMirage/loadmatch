"""merge heads

Revision ID: 69bb0af21ebf
Revises: a2b3c4d5e6f7, 0da14c04b8a5
Create Date: 2026-04-09 09:58:19.044791

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '69bb0af21ebf'
down_revision: Union[str, Sequence[str], None] = ('a2b3c4d5e6f7', '0da14c04b8a5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
