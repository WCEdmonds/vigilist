"""add ingest_jobs table

Revision ID: e7f3a2b91c04
Revises: a1c3e5f82b90
Create Date: 2026-03-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = "e7f3a2b91c04"
down_revision: Union[str, None] = "a1c3e5f82b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ingest_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "production_id",
            sa.Integer,
            sa.ForeignKey("productions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("total_files", sa.Integer, nullable=False, server_default="0"),
        sa.Column("processed_files", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ingest_jobs")
