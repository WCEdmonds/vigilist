"""add production-set conflict override audit fields

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("production_sets", sa.Column("conflicts_overridden_by", sa.String(length=128), nullable=True))
    op.add_column("production_sets", sa.Column("conflicts_overridden_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("production_sets", "conflicts_overridden_at")
    op.drop_column("production_sets", "conflicts_overridden_by")
