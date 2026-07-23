"""add ontology tables (entities, mentions, events, edges, merges)

Revision ID: u3c4d5e6f7g8
Revises: t2b3c4d5e6f7
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "u3c4d5e6f7g8"
down_revision = "t2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("entities_extracted_at", sa.DateTime(), nullable=True))

    op.create_table(
        "entities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(length=10), nullable=False),
        sa.Column("canonical_name", sa.String(length=500), nullable=False),
        sa.Column("aliases", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("attributes", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("overview_generated_at", sa.DateTime(), nullable=True),
        sa.Column("overview_mention_count", sa.Integer(), nullable=True),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_entities_production_id", "entities", ["production_id"])

    op.create_table(
        "entity_mentions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("surface_text", sa.String(length=500), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("context_snippet", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_id", "entity_id", "start_offset", name="uq_mention_doc_entity_offset"),
    )
    op.create_index("ix_entity_mentions_entity_id", "entity_mentions", ["entity_id"])
    op.create_index("ix_entity_mentions_document_id", "entity_mentions", ["document_id"])

    op.create_table(
        "ontology_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("date_precision", sa.String(length=10), nullable=False, server_default="unknown"),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ontology_events_production_id", "ontology_events", ["production_id"])
    op.create_index("ix_ontology_events_document_id", "ontology_events", ["document_id"])

    op.create_table(
        "event_participants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("ontology_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=100), nullable=True),
        sa.UniqueConstraint("event_id", "entity_id", name="uq_event_entity"),
    )
    op.create_index("ix_event_participants_entity_id", "event_participants", ["entity_id"])

    op.create_table(
        "entity_relationships",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relationship_type", sa.String(length=30), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_entity_id", "target_entity_id", "relationship_type", "document_id",
                            name="uq_edge_pair_type_doc"),
    )
    op.create_index("ix_entity_relationships_source", "entity_relationships", ["source_entity_id"])
    op.create_index("ix_entity_relationships_target", "entity_relationships", ["target_entity_id"])

    op.create_table(
        "entity_merge_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_a_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_b_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("entity_a_id", "entity_b_id", name="uq_merge_suggestion_pair"),
    )
    op.create_index("ix_entity_merge_suggestions_production_id", "entity_merge_suggestions", ["production_id"])

    op.create_table(
        "entity_merges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        # SET NULL (not CASCADE): a chain merge (A->B then B->C) deletes B; a
        # cascading FK would also destroy the A->B merge log row (audit +
        # undo history). SET NULL preserves the log row instead.
        sa.Column("winner_entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="SET NULL"), nullable=True),
        sa.Column("loser_snapshot", JSONB(), nullable=False),
        sa.Column("winner_prior", JSONB(), nullable=False),
        sa.Column("moved_mention_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("moved_relationship_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("moved_participant_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("undone", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("merged_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_entity_merges_production_id", "entity_merges", ["production_id"])


def downgrade():
    for name in ("entity_merges", "entity_merge_suggestions", "entity_relationships",
                 "event_participants", "ontology_events", "entity_mentions", "entities"):
        op.drop_table(name)
    op.drop_column("documents", "entities_extracted_at")
