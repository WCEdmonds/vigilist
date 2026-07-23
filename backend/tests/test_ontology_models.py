"""Smoke tests: ontology models exist with the columns later tasks rely on."""


def test_ontology_models_importable():
    from app.models import (
        Entity, EntityMention, OntologyEvent, EventParticipant,
        EntityRelationship, EntityMergeSuggestion, EntityMerge,
    )
    assert Entity.__tablename__ == "entities"
    assert EntityMention.__tablename__ == "entity_mentions"
    assert OntologyEvent.__tablename__ == "ontology_events"
    assert EventParticipant.__tablename__ == "event_participants"
    assert EntityRelationship.__tablename__ == "entity_relationships"
    assert EntityMergeSuggestion.__tablename__ == "entity_merge_suggestions"
    assert EntityMerge.__tablename__ == "entity_merges"


def test_entity_columns():
    from app.models import Entity
    cols = {c.name for c in Entity.__table__.columns}
    assert {"id", "production_id", "entity_type", "canonical_name", "aliases",
            "attributes", "overview", "overview_generated_at",
            "overview_mention_count", "mention_count"} <= cols


def test_document_has_extraction_marker():
    from app.models import Document
    assert "entities_extracted_at" in {c.name for c in Document.__table__.columns}
