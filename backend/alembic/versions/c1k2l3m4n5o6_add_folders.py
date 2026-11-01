"""add folders and folder_documents

Named, per-matter sets of documents reviewers work from (customer: "pull a
subset of docs and put them into a folder, like just ones from a certain
time period").

Revision ID: c1k2l3m4n5o6
Revises: b0j1k2l3m4n5
Create Date: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "c1k2l3m4n5o6"
down_revision = "b0j1k2l3m4n5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "folders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(),
                  sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("production_id", "name", name="uq_folder_name"),
    )
    op.create_index("ix_folders_production_id", "folders", ["production_id"])
    op.create_table(
        "folder_documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("folder_id", sa.Integer(),
                  sa.ForeignKey("folders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_by", sa.String(100), nullable=False),
        sa.Column("added_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("folder_id", "document_id", name="uq_folder_doc"),
    )
    op.create_index("ix_folder_documents_folder_id", "folder_documents", ["folder_id"])
    op.create_index("ix_folder_documents_document_id", "folder_documents", ["document_id"])


def downgrade():
    op.drop_table("folder_documents")
    op.drop_table("folders")
