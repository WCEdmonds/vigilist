"""add production-set render state

Revision ID: b8c9d0e1f2a3
Revises: a9b8c7d6e5f4
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "b8c9d0e1f2a3"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("production_sets", sa.Column("render_status", sa.String(length=20), nullable=False, server_default=sa.text("'not_started'")))
    op.add_column("production_sets", sa.Column("render_error", sa.Text(), nullable=True))
    op.add_column("production_sets", sa.Column("rendered_at", sa.DateTime(), nullable=True))
    op.add_column("production_set_items", sa.Column("output_path", sa.String(length=500), nullable=True))


def downgrade():
    op.drop_column("production_set_items", "output_path")
    op.drop_column("production_sets", "rendered_at")
    op.drop_column("production_sets", "render_error")
    op.drop_column("production_sets", "render_status")
