"""Merge/undo round-trip against a FakeSession."""

import asyncio
import uuid

import pytest

from app.models import Entity, EntityMention, EntityMerge
from app.services.entity_merge import merge_entities, undo_merge
from tests.fakes import FakeResult, FakeSession


def _pair():
    winner = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                    canonical_name="Jorge Rivera", aliases=["J. Rivera"], attributes={}, mention_count=10)
    loser = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                   canonical_name="J Rivera", aliases=["JR"], attributes={}, mention_count=4)
    return winner, loser


def _mentions_for(loser, n=2):
    return [EntityMention(id=100 + i, production_id=1, entity_id=loser.id,
                          document_id=uuid.uuid4(), surface_text="J Rivera",
                          start_offset=i, end_offset=i + 8) for i in range(n)]


def _db(loser_mentions):
    return FakeSession(responders=[
        ("entity_mentions", FakeResult(items=loser_mentions)),
        ("entity_relationships", FakeResult(items=[])),
        ("event_participants", FakeResult(items=[])),
        ("entity_merge_suggestions", FakeResult(items=[])),
    ])


def test_merge_repoints_mentions_and_folds_counts():
    winner, loser = _pair()
    mentions = _mentions_for(loser)
    db = _db(mentions)
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    assert all(m.entity_id == winner.id for m in mentions)
    assert winner.mention_count == 14
    assert "J Rivera" in winner.aliases and "JR" in winner.aliases
    assert merge.loser_snapshot["canonical_name"] == "J Rivera"
    assert merge.moved_mention_ids == [100, 101]
    assert loser in db.deleted


def test_merge_rejects_cross_production():
    winner, loser = _pair()
    loser.production_id = 2
    with pytest.raises(ValueError):
        asyncio.run(merge_entities(_db([]), winner, loser, "u1"))


def test_undo_restores_loser_and_repoints():
    winner, loser = _pair()
    mentions = _mentions_for(loser)
    db = _db(mentions)
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    # undo: mentions are looked up by moved ids
    db2 = FakeSession(
        get_objects={("Entity", winner.id): winner},
        responders=[("entity_mentions", FakeResult(items=mentions)),
                    ("entity_relationships", FakeResult(items=[])),
                    ("event_participants", FakeResult(items=[]))],
    )
    restored = asyncio.run(undo_merge(db2, merge))
    assert restored.canonical_name == "J Rivera"
    assert all(m.entity_id == restored.id for m in mentions)
    assert winner.mention_count == 10
    assert winner.aliases == ["J. Rivera"]
    assert merge.undone is True


def test_undo_twice_raises():
    winner, loser = _pair()
    db = _db(_mentions_for(loser))
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    merge.undone = True
    with pytest.raises(ValueError):
        asyncio.run(undo_merge(FakeSession(get_objects={("Entity", winner.id): winner}), merge))
