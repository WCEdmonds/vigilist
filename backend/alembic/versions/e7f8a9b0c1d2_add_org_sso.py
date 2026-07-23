"""add per-organization SSO provider binding

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("organizations", sa.Column("sso_provider_id", sa.String(length=100), nullable=True))
    op.add_column("organizations", sa.Column("sso_enforced", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade():
    op.drop_column("organizations", "sso_enforced")
    op.drop_column("organizations", "sso_provider_id")
