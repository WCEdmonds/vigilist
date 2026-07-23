"""add production-set package state

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("production_sets", sa.Column("package_status", sa.String(length=20), nullable=False, server_default=sa.text("'not_started'")))
    op.add_column("production_sets", sa.Column("package_error", sa.Text(), nullable=True))
    op.add_column("production_sets", sa.Column("package_path", sa.String(length=500), nullable=True))
    op.add_column("production_sets", sa.Column("packaged_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("production_sets", "packaged_at")
    op.drop_column("production_sets", "package_path")
    op.drop_column("production_sets", "package_error")
    op.drop_column("production_sets", "package_status")
