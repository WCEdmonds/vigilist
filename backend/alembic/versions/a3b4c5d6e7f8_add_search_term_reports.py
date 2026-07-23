"""add search term reports

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "a3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "search_term_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("terms", JSONB(), nullable=False),
        sa.Column("results", JSONB(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_search_term_reports_production_id", "search_term_reports", ["production_id"])


def downgrade():
    op.drop_index("ix_search_term_reports_production_id", table_name="search_term_reports")
    op.drop_table("search_term_reports")
