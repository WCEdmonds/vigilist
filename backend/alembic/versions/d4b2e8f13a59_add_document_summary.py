"""add document summary column

Revision ID: d4b2e8f13a59
Revises: c3a1f9d82e47
Create Date: 2026-03-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd4b2e8f13a59'
down_revision: str = 'c3a1f9d82e47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('summary', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'summary')
