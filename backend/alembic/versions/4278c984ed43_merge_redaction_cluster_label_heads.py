"""merge redaction + cluster-label heads

Revision ID: 4278c984ed43
Revises: 7cffd34cabe8, s1a2b3c4d5e6
Create Date: 2026-07-20 21:48:45.069791

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4278c984ed43'
down_revision: Union[str, Sequence[str], None] = ('7cffd34cabe8', 's1a2b3c4d5e6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
