"""ingest_chunks.cursor: in-flight item position for poison-item isolation

Revision ID: f4o5p6q7r8s9
Revises: e3n4o5p6q7r8
Create Date: 2026-11-03
"""
from alembic import op
import sqlalchemy as sa

revision = "f4o5p6q7r8s9"
down_revision = "e3n4o5p6q7r8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ingest_chunks", sa.Column("cursor", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("ingest_chunks", "cursor")
