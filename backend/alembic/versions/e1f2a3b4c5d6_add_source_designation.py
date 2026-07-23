"""add document source designation

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("source_party", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("source_type", sa.String(length=20), nullable=True))
    op.create_index("ix_documents_source_party", "documents", ["source_party"])
    op.create_index("ix_documents_source_type", "documents", ["source_type"])


def downgrade():
    op.drop_index("ix_documents_source_type", table_name="documents")
    op.drop_index("ix_documents_source_party", table_name="documents")
    op.drop_column("documents", "source_type")
    op.drop_column("documents", "source_party")
