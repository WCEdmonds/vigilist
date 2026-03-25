"""add document title column

Revision ID: c3a1f9d82e47
Revises: 8105f7c0d525
Create Date: 2026-03-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3a1f9d82e47'
down_revision: str = '8105f7c0d525'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('title', sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'title')
