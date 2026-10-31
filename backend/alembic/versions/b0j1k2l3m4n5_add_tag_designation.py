"""add designation to tags

A tag can carry a confidentiality designation ("CONFIDENTIAL", "ATTORNEYS'
EYES ONLY"); locking a production set stamps each document with the
designations of its tags.

Revision ID: b0j1k2l3m4n5
Revises: a9i0j1k2l3m4
Create Date: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa

revision = "b0j1k2l3m4n5"
down_revision = "a9i0j1k2l3m4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tags", sa.Column("designation", sa.String(100), nullable=True))


def downgrade():
    op.drop_column("tags", "designation")
