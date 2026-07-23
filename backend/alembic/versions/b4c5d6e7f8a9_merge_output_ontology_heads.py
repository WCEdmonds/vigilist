"""merge production-output + ontology heads

Both f2a3b4c5d6e7 (P2-5 output options) and u3c4d5e6f7g8 (ontology tables)
revised e1f2a3b4c5d6 from parallel branches; alembic upgrade head fails on
multiple heads until they are joined.

Revision ID: b4c5d6e7f8a9
Revises: f2a3b4c5d6e7, u3c4d5e6f7g8
Create Date: 2026-07-23

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = ('f2a3b4c5d6e7', 'u3c4d5e6f7g8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
