"""add organizations, production.organization_id, seed + backfill thirulaw

Also merges the two prior migration heads (i2d7e6f15g48 and j3e8f7g26h59)
into a single lineage so `alembic upgrade head` is unambiguous again.

Revision ID: k4f9a1b73c80
Revises: i2d7e6f15g48, j3e8f7g26h59
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

# revision identifiers, used by Alembic.
revision = "k4f9a1b73c80"
down_revision = ("i2d7e6f15g48", "j3e8f7g26h59")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("member_role", sa.String(length=20), nullable=False, server_default="reviewer"),
        sa.Column("member_domains", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("creator_emails", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )

    op.add_column(
        "productions",
        sa.Column("organization_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_productions_organization_id",
        "productions",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_productions_organization_id", "productions", ["organization_id"])

    # Seed the Thiru Law organization:
    # - @thirulaw.com users are members with the "manager" role
    # - productions created by @thirulaw.com or wcedmonds28@gmail.com file here
    op.execute(
        """
        INSERT INTO organizations (slug, name, member_role, member_domains, creator_emails)
        VALUES ('thirulaw', 'Thiru Law', 'manager',
                ARRAY['thirulaw.com'], ARRAY['wcedmonds28@gmail.com'])
        """
    )

    # Backfill: every production currently in the system belongs to Thiru Law.
    op.execute(
        """
        UPDATE productions
        SET organization_id = (SELECT id FROM organizations WHERE slug = 'thirulaw')
        WHERE organization_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_productions_organization_id", table_name="productions")
    op.drop_constraint("fk_productions_organization_id", "productions", type_="foreignkey")
    op.drop_column("productions", "organization_id")
    op.drop_table("organizations")
