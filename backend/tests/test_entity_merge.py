"""Merge/undo round-trip against a FakeSession."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity, EntityMention, EntityMerge, EntityRelationship, EventParticipant
from app.schemas import MergeRequest
from app.services.entity_merge import merge_entities, undo_merge
from tests.fakes import FakeResult, FakeSession, FakeUser

# Distinguishing substrings for the collision-preload queries (must be listed
# before the generic "entity_mentions" / "event_participants" responders,
# since those are substrings of every query against those tables).
_WINNER_MENTION_KEYS_SQL = "entity_mentions.document_id, entity_mentions.start_offset"
_WINNER_EDGE_KEYS_SQL = "entity_relationships.relationship_type, entity_relationships.document_id"
_WINNER_EVENT_IDS_SQL = "SELECT event_participants.event_id"
_LIVE_MENTION_COUNT_SQL = "SELECT count(*) AS count_1"


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


def _db(loser_mentions, winner_mention_rows=None, winner_edge_rows=None, winner_event_rows=None, loser_relationships=None, loser_participants=None):
    return FakeSession(responders=[
        (_WINNER_MENTION_KEYS_SQL, FakeResult(rows=winner_mention_rows or [])),
        ("entity_mentions", FakeResult(items=loser_mentions)),
        (_WINNER_EDGE_KEYS_SQL, FakeResult(rows=winner_edge_rows or [])),
        ("entity_relationships", FakeResult(items=loser_relationships or [])),
        (_WINNER_EVENT_IDS_SQL, FakeResult(rows=winner_event_rows or [])),
        ("event_participants", FakeResult(items=loser_participants or [])),
        ("entity_merge_suggestions", FakeResult(items=[])),
    ])


def _counting_responder(*counts):
    """Stateful callable responder: successive calls to the identical
    live-count SQL text return successive values from `counts`."""
    it = iter(counts)
    def _respond(sql):
        return FakeResult(scalar=next(it))
    return _respond


def test_merge_repoints_mentions_and_folds_counts():
    winner, loser = _pair()
    mentions = _mentions_for(loser)
    db = _db(mentions)
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    assert all(m.entity_id == winner.id for m in mentions)
    # 10 (winner's prior) + 2 (mentions actually moved) — NOT + loser.mention_count(4)
    assert winner.mention_count == 12
    assert "J Rivera" in winner.aliases and "JR" in winner.aliases
    assert merge.loser_snapshot["canonical_name"] == "J Rivera"
    assert merge.moved_mention_ids == [100, 101]
    assert loser in db.deleted


def test_merge_rejects_cross_production():
    winner, loser = _pair()
    loser.production_id = 2
    with pytest.raises(ValueError):
        asyncio.run(merge_entities(_db([]), winner, loser, "u1"))


def test_merge_rejects_self_merge():
    winner, _loser = _pair()
    with pytest.raises(ValueError):
        asyncio.run(merge_entities(_db([]), winner, winner, "u1"))


def test_merge_rejects_cross_type():
    winner, loser = _pair()
    loser.entity_type = "org"
    with pytest.raises(ValueError):
        asyncio.run(merge_entities(_db([]), winner, loser, "u1"))


def test_merge_mention_collision_drops_duplicate_and_folds_only_moved_count():
    winner, loser = _pair()
    doc_shared, doc_other = uuid.uuid4(), uuid.uuid4()
    collide = EntityMention(id=300, production_id=1, entity_id=loser.id, document_id=doc_shared,
                            surface_text="J Rivera", start_offset=0, end_offset=8)
    other = EntityMention(id=301, production_id=1, entity_id=loser.id, document_id=doc_other,
                          surface_text="J Rivera", start_offset=5, end_offset=13)
    db = _db([collide, other], winner_mention_rows=[(doc_shared, 0)])
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    assert collide in db.deleted
    assert collide.id not in merge.moved_mention_ids
    assert other.entity_id == winner.id
    assert merge.moved_mention_ids == [301]
    # 10 + 1 moved, not 10 + loser.mention_count(4) and not 10 + 2
    assert winner.mention_count == 11


def test_merge_participant_collision_drops_duplicate_row():
    winner, loser = _pair()
    shared_event_id, other_event_id = 5001, 5002
    shared = EventParticipant(id=200, event_id=shared_event_id, entity_id=loser.id, role=None)
    other = EventParticipant(id=201, event_id=other_event_id, entity_id=loser.id, role=None)
    db = _db([], winner_event_rows=[(shared_event_id,)], loser_participants=[shared, other])
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    assert shared in db.deleted
    assert shared.id not in merge.moved_participant_ids
    assert other.entity_id == winner.id
    assert other.id in merge.moved_participant_ids
    assert merge.moved_participant_ids == [201]


def test_merge_relationship_collision_drops_duplicate_edge():
    winner, loser = _pair()
    third = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                   canonical_name="Alice Smith", aliases=[], attributes={}, mention_count=1)
    doc_shared, doc_other = uuid.uuid4(), uuid.uuid4()
    # Both winner and loser have "colleague" edge to third in same document
    collide = EntityRelationship(id=400, production_id=1, source_entity_id=loser.id,
                                 target_entity_id=third.id, relationship_type="colleague",
                                 document_id=doc_shared, description="works with")
    # Loser has "colleague" edge to third in a different document
    other = EntityRelationship(id=401, production_id=1, source_entity_id=loser.id,
                               target_entity_id=third.id, relationship_type="colleague",
                               document_id=doc_other, description="works with")
    # Winner already has the same edge (source->target, type, doc) as collide will become
    # After collision detection, the edge key will be (winner.id, third.id, "colleague", doc_shared)
    winner_edge_row = (winner.id, third.id, "colleague", doc_shared)
    db = _db([], winner_edge_rows=[winner_edge_row], loser_relationships=[collide, other])
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    assert collide in db.deleted
    assert collide.id not in merge.moved_relationship_ids
    assert other.source_entity_id == winner.id
    assert other.id in merge.moved_relationship_ids
    assert merge.moved_relationship_ids == [401]


def test_undo_restores_loser_and_repoints():
    winner, loser = _pair()
    mentions = _mentions_for(loser)
    db = _db(mentions)
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    # undo: mentions are looked up by moved ids; live counts re-derive to the
    # same totals since no post-merge growth is simulated here.
    db2 = FakeSession(
        get_objects={("Entity", winner.id): winner},
        responders=[(_LIVE_MENTION_COUNT_SQL, _counting_responder(10, 2)),
                    ("entity_mentions", FakeResult(items=mentions)),
                    ("entity_relationships", FakeResult(items=[])),
                    ("event_participants", FakeResult(items=[]))],
    )
    restored = asyncio.run(undo_merge(db2, merge))
    assert restored.canonical_name == "J Rivera"
    assert all(m.entity_id == restored.id for m in mentions)
    assert winner.mention_count == 10
    assert restored.mention_count == 2
    assert winner.aliases == ["J. Rivera"]
    assert merge.undone is True


def test_undo_rederives_counts_and_preserves_post_merge_alias():
    winner, loser = _pair()
    mentions = _mentions_for(loser)
    db = _db(mentions)
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    assert winner.mention_count == 12

    # Simulate post-merge growth that happened between the merge and the
    # undo: a new alias picked up independently, and (via the responder) a
    # new mention ingested against the winner.
    winner.aliases = list(winner.aliases) + ["Jorge R."]

    db2 = FakeSession(
        get_objects={("Entity", winner.id): winner},
        responders=[(_LIVE_MENTION_COUNT_SQL, _counting_responder(15, 2)),
                    ("entity_mentions", FakeResult(items=mentions)),
                    ("entity_relationships", FakeResult(items=[])),
                    ("event_participants", FakeResult(items=[]))],
    )
    restored = asyncio.run(undo_merge(db2, merge))

    # Re-derived from the (mocked) live rows — not clobbered back to the
    # pre-merge snapshot of 10.
    assert winner.mention_count == 15
    assert restored.mention_count == 2
    # Prior alias restored, loser's aliases removed, post-merge addition kept.
    assert winner.aliases == ["J. Rivera", "Jorge R."]
    assert "J Rivera" not in winner.aliases
    assert "JR" not in winner.aliases


def test_undo_twice_raises():
    winner, loser = _pair()
    db = _db(_mentions_for(loser))
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    merge.undone = True
    with pytest.raises(ValueError):
        asyncio.run(undo_merge(FakeSession(get_objects={("Entity", winner.id): winner}), merge))


def test_undo_raises_when_winner_entity_id_is_null():
    """Chain-merge scenario: winner_entity_id was SET NULL because the winner
    was itself merged into something else after this merge was logged."""
    winner, loser = _pair()
    db = _db(_mentions_for(loser))
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    merge.winner_entity_id = None
    with pytest.raises(ValueError):
        asyncio.run(undo_merge(FakeSession(), merge))


# --- endpoint-level: readonly / scoping / concurrent-claim ------------------

class FakeSuggestion:
    def __init__(self, sid, production_id, entity_a_id, entity_b_id, status="pending"):
        self.id = sid
        self.production_id = production_id
        self.entity_a_id = entity_a_id
        self.entity_b_id = entity_b_id
        self.status = status
        self.score = 0.9
        self.rationale = "same name, same production"
        self.resolved_by = None
        self.resolved_at = None


def _patch_scope(monkeypatch, accessible=(1,), role="manager"):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(db, user, action, resource_type, resource_id=None, **kwargs):
        pass

    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(er, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(er, "log_action", fake_log)


def test_accept_suggestion_403_for_readonly(monkeypatch):
    _patch_scope(monkeypatch, role="readonly")
    sugg = FakeSuggestion(1, 1, uuid.uuid4(), uuid.uuid4())
    db = FakeSession(get_objects={("EntityMergeSuggestion", 1): sugg})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.accept_merge_suggestion(suggestion_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_accept_suggestion_404_out_of_scope(monkeypatch):
    _patch_scope(monkeypatch, accessible=(2,))
    sugg = FakeSuggestion(1, 1, uuid.uuid4(), uuid.uuid4())
    db = FakeSession(get_objects={("EntityMergeSuggestion", 1): sugg})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.accept_merge_suggestion(suggestion_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_accept_suggestion_409_when_claim_loses_race(monkeypatch):
    _patch_scope(monkeypatch)
    sugg = FakeSuggestion(1, 1, uuid.uuid4(), uuid.uuid4())
    db = FakeSession(
        get_objects={("EntityMergeSuggestion", 1): sugg},
        responders=[("UPDATE entity_merge_suggestions", FakeResult(rowcount=0))],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.accept_merge_suggestion(suggestion_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_reject_suggestion_409_when_claim_loses_race(monkeypatch):
    _patch_scope(monkeypatch)
    sugg = FakeSuggestion(1, 1, uuid.uuid4(), uuid.uuid4())
    db = FakeSession(
        get_objects={("EntityMergeSuggestion", 1): sugg},
        responders=[("UPDATE entity_merge_suggestions", FakeResult(rowcount=0))],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.reject_merge_suggestion(suggestion_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_reject_suggestion_403_for_readonly(monkeypatch):
    _patch_scope(monkeypatch, role="readonly")
    sugg = FakeSuggestion(1, 1, uuid.uuid4(), uuid.uuid4())
    db = FakeSession(get_objects={("EntityMergeSuggestion", 1): sugg})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.reject_merge_suggestion(suggestion_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_reject_suggestion_logs_audit_action(monkeypatch):
    audit_calls = []

    async def fake_accessible(db, user):
        return [1]

    async def fake_role(db, user, production_id):
        return "manager"

    async def fake_log(db, user, action, resource_type, resource_id=None, **kwargs):
        audit_calls.append((action, resource_type, resource_id, kwargs))

    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(er, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(er, "log_action", fake_log)

    sugg = FakeSuggestion(1, 1, uuid.uuid4(), uuid.uuid4())
    db = FakeSession(
        get_objects={("EntityMergeSuggestion", 1): sugg},
        responders=[("UPDATE entity_merge_suggestions", FakeResult(rowcount=1))],
    )
    out = asyncio.run(er.reject_merge_suggestion(suggestion_id=1, db=db, user=FakeUser()))
    assert out == {"ok": True}
    assert audit_calls == [("entity_merge_suggestion_rejected", "entity_merge_suggestion", "1",
                            {"production_id": 1, "details": {}})]


def test_manual_merge_403_for_readonly(monkeypatch):
    _patch_scope(monkeypatch, role="readonly")
    winner, loser = _pair()
    db = FakeSession(get_objects={("Entity", winner.id): winner, ("Entity", loser.id): loser})
    body = MergeRequest(winner_id=winner.id, loser_id=loser.id)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.manual_merge(body=body, db=db, user=FakeUser()))
    assert exc.value.status_code == 403


# --- retroactive auto-resolve-typos endpoint --------------------------------

# Distinguishing substring for the endpoint's own pending-suggestions query
# (production-scoped), vs. merge_entities' internal per-loser query (which
# filters on entity_a_id/entity_b_id instead — see _WINNER_* substrings and
# "entity_merge_suggestions" generic responder used elsewhere in this file).
_PENDING_BY_PRODUCTION_SQL = "WHERE entity_merge_suggestions.production_id"


def _typo_pair(a_mentions=5, b_mentions=2):
    a = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
              canonical_name="Lynelle Lyles", aliases=[], attributes={}, mention_count=a_mentions)
    b = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
              canonical_name="Lynell Lyles", aliases=[], attributes={}, mention_count=b_mentions)
    return a, b


def _substitution_pair():
    a = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
              canonical_name="John Smith", aliases=[], attributes={}, mention_count=5)
    b = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
              canonical_name="Joan Smith", aliases=[], attributes={}, mention_count=2)
    return a, b


def _merge_internals_responders():
    """Empty collision/collection responders so merge_entities' internal
    queries (mentions/edges/participants/suggestions-touching-loser) all
    resolve to empty results, isolating the test on the typo-detection and
    top-level pending-suggestions query behavior."""
    return [
        (_WINNER_MENTION_KEYS_SQL, FakeResult(rows=[])),
        ("entity_mentions", FakeResult(items=[])),
        (_WINNER_EDGE_KEYS_SQL, FakeResult(rows=[])),
        ("entity_relationships", FakeResult(items=[])),
        (_WINNER_EVENT_IDS_SQL, FakeResult(rows=[])),
        ("event_participants", FakeResult(items=[])),
        ("entity_merge_suggestions", FakeResult(items=[])),
    ]


def test_auto_resolve_typos_merges_typo_pair(monkeypatch):
    _patch_scope(monkeypatch)
    a, b = _typo_pair()
    sugg = FakeSuggestion(1, 1, a.id, b.id)
    db = FakeSession(
        get_objects={("Entity", a.id): a, ("Entity", b.id): b},
        responders=[(_PENDING_BY_PRODUCTION_SQL, FakeResult(items=[sugg]))] + _merge_internals_responders(),
    )
    out = asyncio.run(er.auto_resolve_typo_suggestions(production_id=1, db=db, user=FakeUser()))
    assert out == {"merged": 1}
    # verify the pending-suggestions responder actually fired, not the
    # default-empty fallthrough.
    assert any(_PENDING_BY_PRODUCTION_SQL in sql for sql in db.executed)
    assert b in db.deleted  # a has more mentions (5 > 2) -> a wins, b is folded in


def test_auto_resolve_typos_leaves_substitution_pending(monkeypatch):
    _patch_scope(monkeypatch)
    a, b = _substitution_pair()
    sugg = FakeSuggestion(1, 1, a.id, b.id)
    db = FakeSession(
        get_objects={("Entity", a.id): a, ("Entity", b.id): b},
        responders=[(_PENDING_BY_PRODUCTION_SQL, FakeResult(items=[sugg]))] + _merge_internals_responders(),
    )
    out = asyncio.run(er.auto_resolve_typo_suggestions(production_id=1, db=db, user=FakeUser()))
    assert out == {"merged": 0}
    assert a not in db.deleted and b not in db.deleted
    assert sugg.status == "pending"


def test_auto_resolve_typos_skips_missing_entity_without_raising(monkeypatch):
    _patch_scope(monkeypatch)
    a, _b = _typo_pair()
    missing_id = uuid.uuid4()
    sugg = FakeSuggestion(1, 1, a.id, missing_id)
    db = FakeSession(
        get_objects={("Entity", a.id): a},
        responders=[(_PENDING_BY_PRODUCTION_SQL, FakeResult(items=[sugg]))] + _merge_internals_responders(),
    )
    out = asyncio.run(er.auto_resolve_typo_suggestions(production_id=1, db=db, user=FakeUser()))
    assert out == {"merged": 0}


def test_auto_resolve_typos_skips_suggestion_sharing_consumed_entity(monkeypatch):
    """Chain-merge guard: two pending suggestions share an entity (A~B and
    B~C, both typo pairs). Merging A~B deletes B; the second suggestion must
    be skipped rather than triggering a second merge against the
    already-deleted B (which would corrupt EntityMerge.loser_snapshot and
    later break undo_merge with a primary-key collision)."""
    _patch_scope(monkeypatch)
    a = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
              canonical_name="Lynell Lyles", aliases=[], attributes={}, mention_count=5)
    b = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
              canonical_name="Lynelle Lyles", aliases=[], attributes={}, mention_count=2)
    c = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
              canonical_name="Lynelle Lyle", aliases=[], attributes={}, mention_count=1)
    sugg_ab = FakeSuggestion(1, 1, a.id, b.id)
    sugg_bc = FakeSuggestion(2, 1, b.id, c.id)
    db = FakeSession(
        get_objects={("Entity", a.id): a, ("Entity", b.id): b, ("Entity", c.id): c},
        responders=[
            (_PENDING_BY_PRODUCTION_SQL, FakeResult(items=[sugg_ab, sugg_bc])),
            (_WINNER_MENTION_KEYS_SQL, FakeResult(rows=[])),
            ("entity_mentions", FakeResult(items=[])),
            (_WINNER_EDGE_KEYS_SQL, FakeResult(rows=[])),
            ("entity_relationships", FakeResult(items=[])),
            (_WINNER_EVENT_IDS_SQL, FakeResult(rows=[])),
            ("event_participants", FakeResult(items=[])),
            # merge_entities' own suggestions-touching-loser query, run
            # during the A~B merge (loser=b): a real DB would return sugg_bc
            # here since it's still pending and touches b. Simulate that
            # (instead of _merge_internals_responders()' unconditional
            # empty result) so this test's B~C outcome reflects what
            # production actually does, not a fake-harness artifact.
            ("entity_merge_suggestions", FakeResult(items=[sugg_bc])),
        ],
    )

    calls = []
    real_merge = er.merge_entities

    async def tracking_merge(db, winner, loser, user_id):
        calls.append((winner.id, loser.id))
        return await real_merge(db, winner, loser, user_id)

    monkeypatch.setattr(er, "merge_entities", tracking_merge)

    out = asyncio.run(er.auto_resolve_typo_suggestions(production_id=1, db=db, user=FakeUser()))

    assert out == {"merged": 1}
    # Only the A~B pair was ever passed to merge_entities — the endpoint's
    # own `consumed` guard skips B~C before a second merge_entities call
    # could target the already-deleted B.
    assert calls == [(a.id, b.id)]
    assert b in db.deleted
    assert c not in db.deleted
    # merge_entities' own cleanup pass (which resolves any other pending
    # suggestion touching the loser) fires during the A~B merge and marks
    # B~C "rejected" — it does NOT stay pending in the queue. (Asserting
    # "pending" here previously was an artifact of a fake responder that
    # unconditionally returned no rows for this query; real production
    # behavior resolves it as rejected, per merge_entities' internal
    # suggestions-touching-loser pass.)
    assert sugg_bc.status == "rejected"


def test_auto_resolve_typos_404_out_of_scope(monkeypatch):
    _patch_scope(monkeypatch, accessible=(2,))
    db = FakeSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.auto_resolve_typo_suggestions(production_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_auto_resolve_typos_403_for_readonly(monkeypatch):
    # I2: this endpoint now uses the same _require_writer gate as
    # accept/reject/manual-merge (only readonly is blocked), not a
    # manager-only rank check — a reviewer who can merge by hand must not
    # 403 on the "Auto-merge obvious typos" button.
    _patch_scope(monkeypatch, role="readonly")
    db = FakeSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.auto_resolve_typo_suggestions(production_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_auto_resolve_typos_allows_reviewer_role(monkeypatch):
    _patch_scope(monkeypatch, role="reviewer")
    db = FakeSession(responders=[(_PENDING_BY_PRODUCTION_SQL, FakeResult(items=[]))])
    out = asyncio.run(er.auto_resolve_typo_suggestions(production_id=1, db=db, user=FakeUser()))
    assert out == {"merged": 0}
