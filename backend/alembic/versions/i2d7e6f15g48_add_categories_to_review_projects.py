"""add categories to review_projects

Revision ID: i2d7e6f15g48
Revises: h1c6d5e04f37
Create Date: 2026-03-25

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'i2d7e6f15g48'
down_revision = 'h1c6d5e04f37'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('review_projects', sa.Column('categories', JSONB, nullable=False, server_default='[]'))


def downgrade() -> None:
    op.drop_column('review_projects', 'categories')
