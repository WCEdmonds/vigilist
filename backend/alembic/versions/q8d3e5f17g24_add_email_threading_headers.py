"""add email threading headers (message_id, in_reply_to, email_references)

Revision ID: q8d3e5f17g24
Revises: p7c2d4e06f13
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "q8d3e5f17g24"
down_revision = "p7c2d4e06f13"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("message_id", sa.String(length=500), nullable=True))
    op.add_column("documents", sa.Column("in_reply_to", sa.String(length=500), nullable=True))
    op.add_column("documents", sa.Column("email_references", sa.Text(), nullable=True))
    op.create_index("ix_documents_message_id", "documents", ["message_id"])


def downgrade():
    op.drop_index("ix_documents_message_id", table_name="documents")
    op.drop_column("documents", "email_references")
    op.drop_column("documents", "in_reply_to")
    op.drop_column("documents", "message_id")
