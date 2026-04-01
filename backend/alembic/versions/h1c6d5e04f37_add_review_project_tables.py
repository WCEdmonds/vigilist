"""add review_projects and ai_review_results tables

Revision ID: h1c6d5e04f37
Revises: g9b5c4d03e26
Create Date: 2026-03-25 22:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = 'h1c6d5e04f37'
down_revision: Union[str, None] = 'g9b5c4d03e26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'review_projects',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('production_id', sa.Integer, sa.ForeignKey('productions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('prompt_text', sa.Text, nullable=False),
        sa.Column('prompt_versions', JSONB, nullable=False, server_default='[]'),
        sa.Column('sample_size', sa.Integer, nullable=False, server_default='50'),
        sa.Column('agreement_threshold', sa.Float, nullable=False, server_default='0.8'),
        sa.Column('status', sa.String(20), nullable=False, server_default='draft'),
        sa.Column('total_documents', sa.Integer, nullable=False, server_default='0'),
        sa.Column('processed_documents', sa.Integer, nullable=False, server_default='0'),
        sa.Column('total_cost_tokens', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_by', sa.String(128), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        'ai_review_results',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('project_id', sa.Integer, sa.ForeignKey('review_projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('document_id', UUID(as_uuid=True), sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('is_sample', sa.Integer, nullable=False, server_default='0'),
        sa.Column('ai_decision', sa.String(20), nullable=False),
        sa.Column('confidence_score', sa.Integer, nullable=False),
        sa.Column('reasoning', sa.Text, nullable=False),
        sa.Column('key_excerpts', JSONB, nullable=False, server_default='[]'),
        sa.Column('considerations', sa.Text, nullable=True),
        sa.Column('attorney_decision', sa.String(30), nullable=True),
        sa.Column('attorney_note', sa.Text, nullable=True),
        sa.Column('prompt_version', sa.Integer, nullable=False, server_default='1'),
        sa.Column('api_model', sa.String(50), nullable=False),
        sa.Column('api_cost_tokens', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('project_id', 'document_id', name='uq_project_doc'),
    )
    op.create_index('ix_review_results_project', 'ai_review_results', ['project_id'])
    op.create_index('ix_review_results_confidence', 'ai_review_results', ['project_id', 'confidence_score'])


def downgrade() -> None:
    op.drop_table('ai_review_results')
    op.drop_table('review_projects')
