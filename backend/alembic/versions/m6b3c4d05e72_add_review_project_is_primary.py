"""add is_primary to review_projects

Revision ID: m6b3c4d05e72
Revises: l5a2b3c94d61
Create Date: 2026-07-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'm6b3c4d05e72'
down_revision: str = 'l5a2b3c94d61'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'review_projects',
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    # Backfill: newest project per production becomes primary.
    op.execute(
        """
        UPDATE review_projects rp SET is_primary = true
        WHERE rp.id = (
            SELECT rp2.id FROM review_projects rp2
            WHERE rp2.production_id = rp.production_id
            ORDER BY rp2.created_at DESC LIMIT 1
        )
        """
    )


def downgrade() -> None:
    op.drop_column('review_projects', 'is_primary')
