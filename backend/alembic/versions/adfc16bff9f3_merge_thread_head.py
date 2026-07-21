"""merge thread head

Revision ID: adfc16bff9f3
Revises: 9a6ac2590389, r9e4f6g28h35
Create Date: 2026-07-20 20:54:45.963916

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'adfc16bff9f3'
down_revision: Union[str, Sequence[str], None] = ('9a6ac2590389', 'r9e4f6g28h35')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
