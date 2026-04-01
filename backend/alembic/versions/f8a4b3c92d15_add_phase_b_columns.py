"""add raw_image_paths and processing_status to documents

Revision ID: f8a4b3c92d15
Revises: e7f3a2b91c04
Create Date: 2026-03-25 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'f8a4b3c92d15'
down_revision: Union[str, None] = '1c9659e5cf78'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('raw_image_paths', JSONB, nullable=False, server_default='[]'))
    op.add_column('documents', sa.Column('processing_status', sa.String(20), nullable=False, server_default='complete'))
    # Existing documents already have converted images, so default to 'complete'


def downgrade() -> None:
    op.drop_column('documents', 'processing_status')
    op.drop_column('documents', 'raw_image_paths')
