"""add timestamp to notes

Revision ID: 632fe3442ef8
Revises: i2d7e6f15g48
Create Date: 2026-03-26 19:20:41.184756

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '632fe3442ef8'
down_revision: Union[str, Sequence[str], None] = 'i2d7e6f15g48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('notes', sa.Column('timestamp', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('notes', 'timestamp')
