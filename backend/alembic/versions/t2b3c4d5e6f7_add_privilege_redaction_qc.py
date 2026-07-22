"""add privilege flags + redaction qc decisions

Revision ID: t2b3c4d5e6f7
Revises: 4278c984ed43
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "t2b3c4d5e6f7"
down_revision = "4278c984ed43"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tags", sa.Column("is_privilege", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("documents", sa.Column("privilege_disposition", sa.String(length=20), nullable=True))
    op.add_column("documents", sa.Column("privilege_description", sa.Text(), nullable=True))
    op.create_table(
        "redaction_qc_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("redaction_count", sa.Integer(), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column("decided_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_rqc_document_id", "redaction_qc_decisions", ["document_id"])


def downgrade():
    op.drop_index("ix_rqc_document_id", table_name="redaction_qc_decisions")
    op.drop_table("redaction_qc_decisions")
    op.drop_column("documents", "privilege_description")
    op.drop_column("documents", "privilege_disposition")
    op.drop_column("tags", "is_privilege")
