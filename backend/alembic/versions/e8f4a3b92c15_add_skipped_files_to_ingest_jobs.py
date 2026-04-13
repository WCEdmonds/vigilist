"""add skipped_files to ingest_jobs

Revision ID: e8f4a3b92c15
Revises: d51df1616ba8
Create Date: 2026-04-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e8f4a3b92c15"
down_revision: Union[str, None] = "d51df1616ba8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingest_jobs",
        sa.Column("skipped_files", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ingest_jobs", "skipped_files")
