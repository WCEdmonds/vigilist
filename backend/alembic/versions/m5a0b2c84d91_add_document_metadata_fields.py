"""add typed metadata columns to documents + field_mapping to ingest_jobs

Revision ID: m5a0b2c84d91
Revises: k4f9a1b73c80
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "m5a0b2c84d91"
down_revision = "k4f9a1b73c80"
branch_labels = None
depends_on = None

_DOC_COLUMNS = [
    ("custodian", sa.String(length=255)),
    ("date_sent", sa.DateTime(timezone=True)),
    ("date_received", sa.DateTime(timezone=True)),
    ("date_created", sa.DateTime(timezone=True)),
    ("date_modified", sa.DateTime(timezone=True)),
    ("file_hash_md5", sa.String(length=32)),
    ("file_hash_sha256", sa.String(length=64)),
    ("file_type", sa.String(length=50)),
    ("file_name", sa.String(length=500)),
    ("source_path", sa.String(length=1000)),
    ("extraction_error", sa.Text()),
    ("email_from", sa.String(length=500)),
    ("email_to", sa.Text()),
    ("email_cc", sa.Text()),
    ("email_bcc", sa.Text()),
    ("email_subject", sa.String(length=1000)),
]


def upgrade() -> None:
    for name, type_ in _DOC_COLUMNS:
        op.add_column("documents", sa.Column(name, type_, nullable=True))
    op.add_column(
        "documents",
        sa.Column("extraction_status", sa.String(length=20), nullable=False, server_default="ok"),
    )
    op.add_column(
        "ingest_jobs",
        sa.Column("field_mapping", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_documents_custodian", "documents", ["custodian"])
    op.create_index("ix_documents_date_sent", "documents", ["date_sent"])
    op.create_index("ix_documents_file_hash_sha256", "documents", ["file_hash_sha256"])
    op.create_index("ix_documents_file_type", "documents", ["file_type"])


def downgrade() -> None:
    op.drop_index("ix_documents_file_type", table_name="documents")
    op.drop_index("ix_documents_file_hash_sha256", table_name="documents")
    op.drop_index("ix_documents_date_sent", table_name="documents")
    op.drop_index("ix_documents_custodian", table_name="documents")
    op.drop_column("ingest_jobs", "field_mapping")
    op.drop_column("documents", "extraction_status")
    for name, _ in reversed(_DOC_COLUMNS):
        op.drop_column("documents", name)
