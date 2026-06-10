"""add source_format to ingest_jobs

Revision ID: j3e8f7g26h59
Revises: i2d7e6f15g48
Create Date: 2026-06-09

"""
from alembic import op
import sqlalchemy as sa

revision = "j3e8f7g26h59"
down_revision = "e8f4a3b92c15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingest_jobs",
        sa.Column(
            "source_format",
            sa.String(length=20),
            nullable=False,
            server_default="relativity",
        ),
    )


def downgrade() -> None:
    op.drop_column("ingest_jobs", "source_format")
