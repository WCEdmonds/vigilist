"""merge redesign and ingest heads

Revision ID: 9a6ac2590389
Revises: n7c4d5e06f83, p7c2d4e06f13
Create Date: 2026-07-20 20:41:33.985306

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a6ac2590389'
down_revision: Union[str, Sequence[str], None] = ('n7c4d5e06f83', 'p7c2d4e06f13')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
