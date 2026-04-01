"""add document_chunks table with pgvector

Revision ID: g9b5c4d03e26
Revises: 81123c61b10e
Create Date: 2026-03-25 21:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = 'g9b5c4d03e26'
down_revision: Union[str, None] = '81123c61b10e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table(
        'document_chunks',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('document_id', sa.UUID(as_uuid=True), sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chunk_index', sa.Integer, nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('embedding', Vector(512), nullable=False),
        sa.UniqueConstraint('document_id', 'chunk_index', name='uq_chunk_doc_idx'),
    )
    op.create_index('ix_chunks_document_id', 'document_chunks', ['document_id'])
    op.execute("""
        CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    op.drop_table('document_chunks')
