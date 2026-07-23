"""add production sets + items

Revision ID: a9b8c7d6e5f4
Revises: t2b3c4d5e6f7
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a9b8c7d6e5f4"
down_revision = "t2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "production_sets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("prefix", sa.String(length=50), nullable=False),
        sa.Column("padding", sa.Integer(), nullable=False, server_default=sa.text("6")),
        sa.Column("start_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("sort_key", sa.String(length=30), nullable=False, server_default=sa.text("'control_number'")),
        sa.Column("designation", sa.String(length=100), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("production_id", "name", name="uq_prodset_name"),
    )
    op.create_index("ix_production_sets_production_id", "production_sets", ["production_id"])
    op.create_table(
        "production_set_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_set_id", sa.Integer(), sa.ForeignKey("production_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("bates_begin", sa.String(length=50), nullable=True),
        sa.Column("bates_end", sa.String(length=50), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("disposition", sa.String(length=20), nullable=True),
        sa.Column("designation", sa.String(length=100), nullable=True),
        sa.UniqueConstraint("production_set_id", "document_id", name="uq_prodset_item_doc"),
    )
    op.create_index("ix_prodset_items_set_id", "production_set_items", ["production_set_id"])
    op.create_index("ix_prodset_items_document_id", "production_set_items", ["document_id"])


def downgrade():
    op.drop_index("ix_prodset_items_document_id", table_name="production_set_items")
    op.drop_index("ix_prodset_items_set_id", table_name="production_set_items")
    op.drop_table("production_set_items")
    op.drop_index("ix_production_sets_production_id", table_name="production_sets")
    op.drop_table("production_sets")
