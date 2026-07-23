"""persist_extraction against a FakeSession: entity creation, attach,
suggestion creation, mention offsets, event + relationship wiring."""

import asyncio
import uuid

from app.models import Entity, EntityMention, EntityMergeSuggestion, EntityRelationship, OntologyEvent
from app.services.entity_extraction import header_candidates, persist_extraction
from tests.fakes import FakeResult, FakeSession

DOC_ID = uuid.uuid4()
TEXT = "Jorge Rivera emailed Ana Cruz about the Acme Corp merger. Rivera signed."


def _parsed(entities=None, events=None, relationships=None):
    return {"entities": entities or [], "events": events or [], "relationships": relationships or []}


def _session_with_existing(entities):
    # persist_extraction loads existing production entities with one SELECT on "entities"
    return FakeSession(responders=[("FROM entities", FakeResult(items=entities))])


def test_creates_entities_and_offset_mentions():
    db = _session_with_existing([])
    parsed = _parsed(entities=[
        {"name": "Jorge Rivera", "type": "person", "surface_forms": ["Jorge Rivera", "Rivera"], "role": None, "emails": []},
        {"name": "Acme Corp", "type": "org", "surface_forms": ["Acme Corp"], "role": None, "emails": []},
    ])
    stats = asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    ents = [o for o in db.added if isinstance(o, Entity)]
    mentions = [o for o in db.added if isinstance(o, EntityMention)]
    assert stats["entities"] == 2 and len(ents) == 2
    assert stats["mentions"] == len(mentions) == 3  # 2x Rivera-forms + 1x Acme
    for m in mentions:
        assert TEXT[m.start_offset:m.end_offset] == m.surface_text


def test_attaches_to_existing_and_appends_alias():
    existing = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                      canonical_name="Jorge Rivera", aliases=[], attributes={}, mention_count=5)
    db = _session_with_existing([existing])
    parsed = _parsed(entities=[{"name": "jorge rivera", "type": "person",
                                "surface_forms": ["Rivera"], "role": None, "emails": []}])
    asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    assert not [o for o in db.added if isinstance(o, Entity)]  # no new entity
    assert "Rivera" in existing.aliases
    # "Rivera" occurs twice in TEXT (inside "Jorge Rivera" and standalone) — with
    # only the bare form supplied, both count: 5 + 2.
    assert existing.mention_count == 7


def test_borderline_creates_entity_plus_suggestion():
    existing = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                      canonical_name="J. Rivera", aliases=[], attributes={}, mention_count=1)
    db = _session_with_existing([existing])
    parsed = _parsed(entities=[{"name": "Jorge Rivera", "type": "person",
                                "surface_forms": ["Jorge Rivera"], "role": None, "emails": []}])
    stats = asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    assert stats["entities"] == 1 and stats["suggestions"] == 1
    sugg = [o for o in db.added if isinstance(o, EntityMergeSuggestion)]
    assert len(sugg) == 1 and sugg[0].status == "pending"


def test_events_and_relationships_link_created_entities():
    db = _session_with_existing([])
    parsed = _parsed(
        entities=[
            {"name": "Jorge Rivera", "type": "person", "surface_forms": ["Jorge Rivera"], "role": None, "emails": []},
            {"name": "Acme Corp", "type": "org", "surface_forms": ["Acme Corp"], "role": None, "emails": []},
        ],
        events=[{"description": "Merger discussion", "type": "communication", "date": "2019-03",
                 "participants": ["Jorge Rivera", "Acme Corp", "Nobody Known"]}],
        relationships=[{"source": "Jorge Rivera", "target": "Acme Corp", "type": "employment", "evidence": "sig"}],
    )
    stats = asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    events = [o for o in db.added if isinstance(o, OntologyEvent)]
    edges = [o for o in db.added if isinstance(o, EntityRelationship)]
    assert stats["events"] == 1 and events[0].date_precision == "month"
    assert len(events[0].participants) == 2  # unknown participant skipped
    assert stats["relationships"] == 1 and edges[0].relationship_type == "employment"


def test_dedupes_duplicate_mentions_across_candidates_resolving_to_same_entity():
    # Two candidates that both resolve (attach) to the same existing entity
    # with overlapping surface forms would otherwise rediscover the same
    # offsets twice, producing duplicate (document_id, entity_id, start_offset)
    # rows that violate uq_mention_doc_entity_offset (regression for FINDING 1).
    existing = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                      canonical_name="Jorge Rivera", aliases=["Rivera"], attributes={}, mention_count=0)
    db = _session_with_existing([existing])
    parsed = _parsed(entities=[
        {"name": "Jorge Rivera", "type": "person", "surface_forms": ["Jorge Rivera", "Rivera"], "role": None, "emails": []},
        {"name": "Rivera", "type": "person", "surface_forms": ["Jorge Rivera", "Rivera"], "role": None, "emails": []},
    ])
    stats = asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    mentions = [o for o in db.added if isinstance(o, EntityMention)]
    keys = [(m.entity_id, m.start_offset) for m in mentions]
    assert len(keys) == len(set(keys))  # no duplicate (entity_id, start_offset) pairs
    assert stats["mentions"] == len(mentions)
    assert existing.mention_count == len(mentions)


def test_header_candidates_from_email_metadata():
    class Doc:
        email_from = "Jorge Rivera <jr@acme.com>"
        email_to = "ana@firm.law"
        email_cc = None
        email_bcc = None
    cands = header_candidates(Doc())
    names = {c["name"] for c in cands}
    assert "Jorge Rivera" in names and "ana" in names  # bare address falls back to local-part
    assert all(c["type"] == "person" for c in cands)
