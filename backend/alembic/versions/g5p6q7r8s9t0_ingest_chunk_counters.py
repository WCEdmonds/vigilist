"""ingest_chunks: persisted pre-claim failures and poison-cursor hits

Revision ID: g5p6q7r8s9t0
Revises: f4o5p6q7r8s9
Create Date: 2026-11-03
"""
from alembic import op
import sqlalchemy as sa

revision = "g5p6q7r8s9t0"
down_revision = "f4o5p6q7r8s9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ingest_chunks", sa.Column(
        "preclaim_failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ingest_chunks", sa.Column(
        "cursor_hits", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("ingest_chunks", "cursor_hits")
    op.drop_column("ingest_chunks", "preclaim_failures")
