"""add done_keys to ingest_jobs and email_body_html to documents

Native containers (mbox/pst/zip) expand to many documents, so job completion
can't compare processed_files (documents) against total_files (source files).
Each fully ingested source file is keyed here; completion counts distinct
done/skipped keys.

documents.email_body_html keeps each message's original HTML body for the
email preview (previously only a stripped text rendering was stored).

Revision ID: a9i0j1k2l3m4
Revises: z8h9i0j1k2l3
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "a9i0j1k2l3m4"
down_revision = "z8h9i0j1k2l3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ingest_jobs",
        sa.Column(
            "done_keys",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


    op.add_column("documents", sa.Column("email_body_html", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("documents", "email_body_html")
    op.drop_column("ingest_jobs", "done_keys")
