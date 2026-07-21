"""null out refusal-sentence cluster labels

Revision ID: 7cffd34cabe8
Revises: adfc16bff9f3
Create Date: 2026-07-20 21:20:29.588777

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7cffd34cabe8'
down_revision: Union[str, Sequence[str], None] = 'adfc16bff9f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Null out cluster labels that are model refusal sentences, not labels.

    The label generator sometimes answered with an apology sentence on
    OCR-poor clusters ("I cannot reliably extract legible document
    titles…"), which then rendered inside theme chips. NULL labels display
    as "Cluster N". Plain SQL only — this runs under the deploy workflow's
    minimal dependency set.
    """
    op.execute(
        sa.text(
            """
            UPDATE document_clusters
            SET label = NULL
            WHERE label IS NOT NULL AND (
                length(label) > 40
                OR label ILIKE '%i cannot%'
                OR label ILIKE '%i can''t%'
                OR label ILIKE '%unable to%'
                OR label ILIKE '%illegible%'
                OR label ILIKE '%not legible%'
                OR label ILIKE '%sorry%'
                OR label = 'Unlabeled'
            )
            """
        )
    )


def downgrade() -> None:
    """Data cleanup is not reversible; nothing to restore."""
    pass
