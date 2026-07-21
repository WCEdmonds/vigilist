"""add redactions table

Revision ID: s1a2b3c4d5e6
Revises: adfc16bff9f3
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "s1a2b3c4d5e6"
down_revision = "adfc16bff9f3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "redactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_num", sa.Integer(), nullable=False),
        sa.Column("x_pct", sa.Float(), nullable=False),
        sa.Column("y_pct", sa.Float(), nullable=False),
        sa.Column("w_pct", sa.Float(), nullable=False),
        sa.Column("h_pct", sa.Float(), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_redactions_document_id", "redactions", ["document_id"])
    op.create_index("ix_redactions_doc_page", "redactions", ["document_id", "page_num"])


def downgrade():
    op.drop_index("ix_redactions_doc_page", table_name="redactions")
    op.drop_index("ix_redactions_document_id", table_name="redactions")
    op.drop_table("redactions")
