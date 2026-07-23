"""add production output options (image format, native types, volumes)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("production_sets", sa.Column("image_format", sa.String(length=10), nullable=False, server_default=sa.text("'pdf'")))
    op.add_column("production_sets", sa.Column("native_file_types", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("production_sets", sa.Column("volume_max_mb", sa.Integer(), nullable=True))
    op.add_column("production_set_items", sa.Column("produce_native", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade():
    op.drop_column("production_set_items", "produce_native")
    op.drop_column("production_sets", "volume_max_mb")
    op.drop_column("production_sets", "native_file_types")
    op.drop_column("production_sets", "image_format")
