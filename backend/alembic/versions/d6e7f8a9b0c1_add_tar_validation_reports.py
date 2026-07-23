"""add tar validation reports

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tar_validation_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("review_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("results", JSONB(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tar_validation_reports_production_id", "tar_validation_reports", ["production_id"])


def downgrade():
    op.drop_index("ix_tar_validation_reports_production_id", table_name="tar_validation_reports")
    op.drop_table("tar_validation_reports")
