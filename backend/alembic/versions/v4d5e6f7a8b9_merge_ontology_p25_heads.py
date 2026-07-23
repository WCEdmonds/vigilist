"""merge ontology and p2-5 output-options heads

Both u3c4d5e6f7g8 (ontology tables, PR #48) and f2a3b4c5d6e7 (production
output options, PR #47) were parented on e1f2a3b4c5d6 in parallel branches;
this empty merge revision rejoins them into a single head.

Revision ID: v4d5e6f7a8b9
Revises: u3c4d5e6f7g8, f2a3b4c5d6e7
Create Date: 2026-07-23
"""

revision = "v4d5e6f7a8b9"
down_revision = ("u3c4d5e6f7g8", "f2a3b4c5d6e7")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
