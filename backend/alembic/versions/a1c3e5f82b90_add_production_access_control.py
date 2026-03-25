"""add production access control

Revision ID: a1c3e5f82b90
Revises: 8f65b7730b6d
Create Date: 2026-03-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1c3e5f82b90"
down_revision: Union[str, None] = "8f65b7730b6d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add owner_id to productions
    op.add_column(
        "productions",
        sa.Column("owner_id", sa.String(128), sa.ForeignKey("users.id"), nullable=True),
    )

    # Create production_access table
    op.create_table(
        "production_access",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "production_id",
            sa.Integer,
            sa.ForeignKey("productions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            sa.String(128),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("production_id", "user_id", name="uq_prod_user"),
    )

    # Create pending_invites table
    op.create_table(
        "pending_invites",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "production_id",
            sa.Integer,
            sa.ForeignKey("productions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column(
            "invited_by",
            sa.String(128),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("production_id", "email", name="uq_prod_email_invite"),
    )


def downgrade() -> None:
    op.drop_table("pending_invites")
    op.drop_table("production_access")
    op.drop_column("productions", "owner_id")
