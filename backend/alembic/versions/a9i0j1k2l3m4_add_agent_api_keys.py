"""add agent_api_keys

Machine credentials for autonomous agents calling /api/agent/*. An agent is
not a User: it has no Firebase identity, so it cannot ride the normal
ID-token path, and it must be revocable without disturbing anyone's login.

Scope is baked into the row rather than requested per call — a key is minted
for exactly one production, optionally narrowed to one production set — so a
leaked token cannot be pointed at a different matter. Only the SHA-256 of the
token is stored; `key_prefix` exists so a key can be located (and displayed)
without holding the secret.

Revision ID: a9i0j1k2l3m4
Revises: z8h9i0j1k2l3
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa

revision = "a9i0j1k2l3m4"
down_revision = "z8h9i0j1k2l3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_api_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("key_prefix", sa.String(24), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "production_id",
            sa.Integer(),
            sa.ForeignKey("productions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "production_set_id",
            sa.Integer(),
            sa.ForeignKey("production_sets.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "role", sa.String(20), nullable=False, server_default="readonly"
        ),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "revoked_by", sa.String(128), sa.ForeignKey("users.id"), nullable=True
        ),
    )
    # Serves authentication: every request looks a key up by its prefix before
    # doing the constant-time hash comparison.
    op.create_index("ix_agent_api_keys_prefix", "agent_api_keys", ["key_prefix"])
    # Serves the management listing: keys issued for this matter.
    op.create_index(
        "ix_agent_api_keys_production_id", "agent_api_keys", ["production_id"]
    )


def downgrade():
    op.drop_index("ix_agent_api_keys_production_id", table_name="agent_api_keys")
    op.drop_index("ix_agent_api_keys_prefix", table_name="agent_api_keys")
    op.drop_table("agent_api_keys")
