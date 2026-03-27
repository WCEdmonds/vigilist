"""add duplicate and cluster tables

Revision ID: d51df1616ba8
Revises: 632fe3442ef8
Create Date: 2026-03-26 21:54:43.706913

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd51df1616ba8'
down_revision: Union[str, Sequence[str], None] = '632fe3442ef8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('document_clusters',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('production_id', sa.Integer(), nullable=False),
    sa.Column('cluster_index', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.Column('doc_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['production_id'], ['productions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('duplicate_groups',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('production_id', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['production_id'], ['productions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('document_cluster_assignments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('cluster_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['cluster_id'], ['document_clusters.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', name='uq_doc_cluster')
    )
    op.create_index('ix_doc_cluster_assignments_document_id', 'document_cluster_assignments', ['document_id'], unique=False)
    op.create_table('document_duplicates',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.Column('similarity', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['group_id'], ['duplicate_groups.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'group_id', name='uq_doc_dup_group')
    )
    op.create_index('ix_document_duplicates_document_id', 'document_duplicates', ['document_id'], unique=False)
    op.create_index('ix_document_duplicates_group_id', 'document_duplicates', ['group_id'], unique=False)
    op.add_column('documents', sa.Column('family_id', sa.String(length=255), nullable=True))
    op.add_column('documents', sa.Column('thread_id', sa.String(length=255), nullable=True))
    op.add_column('documents', sa.Column('is_inclusive', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'is_inclusive')
    op.drop_column('documents', 'thread_id')
    op.drop_column('documents', 'family_id')
    op.drop_index('ix_document_duplicates_group_id', table_name='document_duplicates')
    op.drop_index('ix_document_duplicates_document_id', table_name='document_duplicates')
    op.drop_table('document_duplicates')
    op.drop_index('ix_doc_cluster_assignments_document_id', table_name='document_cluster_assignments')
    op.drop_table('document_cluster_assignments')
    op.drop_table('duplicate_groups')
    op.drop_table('document_clusters')
