"""large container ingest: ingest_chunks, job item counters, production ai_hold

Revision ID: e3n4o5p6q7r8
Revises: d2l3m4n5o6p7
Create Date: 2026-11-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "e3n4o5p6q7r8"
down_revision = "d2l3m4n5o6p7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ingest_jobs", sa.Column("survey", JSONB(), nullable=False, server_default="{}"))
    op.add_column("ingest_jobs", sa.Column("selection", JSONB(), nullable=False, server_default="{}"))
    for col in ("items_total", "items_done", "items_failed"):
        op.add_column("ingest_jobs", sa.Column(col, sa.Integer(), nullable=False, server_default="0"))
    op.add_column("productions", sa.Column("ai_hold", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "ingest_chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("ingest_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("container_path", sa.String(500), nullable=True),
        sa.Column("slices", JSONB(), nullable=False, server_default="[]"),
        sa.Column("doc_ids", JSONB(), nullable=False, server_default="[]"),
        sa.Column("type_filter", JSONB(), nullable=False, server_default="[]"),
        sa.Column("global_offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ingest_chunks_job_id", "ingest_chunks", ["job_id"])
    op.create_index("ix_ingest_chunks_job_status", "ingest_chunks", ["job_id", "status"])


def downgrade():
    op.drop_index("ix_ingest_chunks_job_status", table_name="ingest_chunks")
    op.drop_index("ix_ingest_chunks_job_id", table_name="ingest_chunks")
    op.drop_table("ingest_chunks")
    op.drop_column("productions", "ai_hold")
    for col in ("items_failed", "items_done", "items_total", "selection", "survey"):
        op.drop_column("ingest_jobs", col)
