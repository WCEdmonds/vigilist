"""imaging state on documents; redaction count snapshot on production set items

Emails and PDF/image attachments are imaged on demand so they can be
redacted; production set items record their redaction count at lock so a
render can refuse redactions that changed afterwards.

Revision ID: d2l3m4n5o6p7
Revises: c1k2l3m4n5o6
Create Date: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "d2l3m4n5o6p7"
down_revision = "c1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("imaging_status", sa.String(20), nullable=True))
    op.add_column("documents", sa.Column("imaging_error", sa.String(500), nullable=True))
    op.add_column("documents", sa.Column("imaging_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("imaged_from", sa.String(20), nullable=True))
    op.add_column("documents", sa.Column("header_regions", JSONB(), nullable=True))
    op.add_column("production_set_items", sa.Column("redaction_count", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("production_set_items", "redaction_count")
    for col in ("header_regions", "imaged_from", "imaging_requested_at", "imaging_error", "imaging_status"):
        op.drop_column("documents", col)
