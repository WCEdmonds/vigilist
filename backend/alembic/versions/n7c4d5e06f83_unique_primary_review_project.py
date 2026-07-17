"""unique primary review project

Revision ID: n7c4d5e06f83
Revises: m6b3c4d05e72
Create Date: 2026-07-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'n7c4d5e06f83'
down_revision: str = 'm6b3c4d05e72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'uq_review_projects_primary_per_production',
        'review_projects',
        ['production_id'],
        unique=True,
        postgresql_where=sa.text('is_primary'),
    )


def downgrade() -> None:
    op.drop_index('uq_review_projects_primary_per_production', table_name='review_projects')
