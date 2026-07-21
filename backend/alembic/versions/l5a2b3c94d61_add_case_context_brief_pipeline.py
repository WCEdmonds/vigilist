"""add case_context, brief, ai_pipeline_status to productions

Revision ID: l5a2b3c94d61
Revises: k4f9a1b73c80
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'l5a2b3c94d61'
down_revision: str = 'k4f9a1b73c80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('productions', sa.Column('case_context', sa.Text(), nullable=True))
    op.add_column('productions', sa.Column('brief', JSONB(), nullable=True))
    op.add_column('productions', sa.Column('ai_pipeline_status', JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('productions', 'ai_pipeline_status')
    op.drop_column('productions', 'brief')
    op.drop_column('productions', 'case_context')
