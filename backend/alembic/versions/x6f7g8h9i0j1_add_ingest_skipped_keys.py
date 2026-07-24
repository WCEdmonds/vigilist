"""add skipped_keys to ingest_jobs

Skip counts must be idempotent across Cloud Tasks retries: each skip event is
keyed to a stable per-record identity stored here, and the increment is a
single guarded UPDATE that no-ops when the key was already counted.

Revision ID: x6f7g8h9i0j1
Revises: w5e6f7g8h9i0
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "x6f7g8h9i0j1"
down_revision = "w5e6f7g8h9i0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ingest_jobs",
        sa.Column(
            "skipped_keys",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("ingest_jobs", "skipped_keys")
