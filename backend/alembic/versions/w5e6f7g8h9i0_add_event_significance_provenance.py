"""add significance + date provenance to ontology_events

Revision ID: w5e6f7g8h9i0
Revises: e7f8a9b0c1d2
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa

revision = "w5e6f7g8h9i0"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade():
    # Both nullable: the improved extractor re-run populates these; no backfill.
    # significance 1 (routine) .. 5 (pivotal); null = unrated.
    op.add_column("ontology_events", sa.Column("significance", sa.SmallInteger(), nullable=True))
    # date_source_text: verbatim phrase the date came from (provenance); null when
    # undated/unsourced.
    op.add_column("ontology_events", sa.Column("date_source_text", sa.Text(), nullable=True))
    op.create_index("ix_ontology_events_production_significance", "ontology_events",
                    ["production_id", "significance"])


def downgrade():
    op.drop_index("ix_ontology_events_production_significance", table_name="ontology_events")
    op.drop_column("ontology_events", "date_source_text")
    op.drop_column("ontology_events", "significance")
