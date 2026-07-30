"""AI merge review: verdict parsing and application.

Fake-session unit tests, no DB or network.

The load-bearing test here is
test_co_occurrence_does_not_block_a_merge. An earlier draft of this design
treated two names appearing in the same document as near-conclusive evidence
of *distinct* people, with a code-level guard to match. In an OCR'd corpus
that is backwards — the correct spelling and its corruption routinely appear
in the same document — so the guard would have rejected exactly the merges
the feature exists to make.
"""

import asyncio
import uuid

import pytest

import app.services.merge_review as mr
from app.services.merge_review import CONFIDENCE_GATE, apply_verdicts, parse_verdicts


class FakeUser:
    id = "u1"
    email = "reviewer@firm.com"


class FakeEntity:
    def __init__(self, mention_count=10, name="Tate Sterling"):
        self.id = uuid.uuid4()
        self.production_id = 1
        self.entity_type = "person"
        self.canonical_name = name
        self.aliases = []
        self.attributes = {}
        self.mention_count = mention_count


class FakeSuggestion:
    def __init__(self, sid, a, b, production_id=1, status="pending"):
        self.id = sid
        self.production_id = production_id
        self.entity_a_id = a.id
        self.entity_b_id = b.id
        self.score = 0.93
        self.rationale = "name similarity 0.93"
        self.status = status
        self.resolved_by = None
        self.resolved_at = None


class FakeSession:
    def __init__(self, objects):
        self._objects = objects

    async def get(self, model, key):
        return self._objects.get(key)


def _setup(monkeypatch, mention_a=40, mention_b=2, status="pending"):
    """One pending suggestion over two entities, with merge_entities stubbed."""
    a = FakeEntity(mention_count=mention_a, name="Tate Sterling")
    b = FakeEntity(mention_count=mention_b, name="Tate Streling")
    s = FakeSuggestion(1, a, b, status=status)
    db = FakeSession({a.id: a, b.id: b, 1: s})

    calls = []

    async def fake_merge(db_, winner, loser, user_id):
        calls.append((winner, loser, user_id))
        return object()

    monkeypatch.setattr(mr, "merge_entities", fake_merge)
    return db, s, a, b, calls


# ── parse_verdicts ───────────────────────────────────────────────────────

def test_parse_rejects_unusable_output_rather_than_guessing():
    assert parse_verdicts("not json") == []
    assert parse_verdicts("[]") == []                      # not an object
    assert parse_verdicts('{"verdicts": null}') == []


def test_parse_drops_entries_missing_required_fields():
    raw = ('{"verdicts": ['
           '{"suggestion_id": 1, "verdict": "merge", "reason": "x"},'          # no confidence
           '{"verdict": "merge", "reason": "x", "confidence": 0.9},'           # no id
           '{"suggestion_id": 3, "verdict": "maybe", "reason": "x", "confidence": 0.9},'
           '{"suggestion_id": 4, "verdict": "merge", "reason": "ok", "confidence": 0.91}]}')
    out = parse_verdicts(raw)
    assert [v["suggestion_id"] for v in out] == [4]


# ── apply_verdicts ───────────────────────────────────────────────────────

def test_merge_above_the_gate_is_applied(monkeypatch):
    db, s, a, b, calls = _setup(monkeypatch)
    out = asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "merge", "keep_id": str(a.id),
        "reason": "OCR transposition", "confidence": 0.95,
    }], FakeUser()))

    assert out["merged"] == 1
    assert len(calls) == 1
    winner, loser, _ = calls[0]
    assert winner is a and loser is b           # model's keep_id honoured
    assert s.status == "accepted"


def test_merge_below_the_gate_is_left_pending_and_annotated(monkeypatch):
    db, s, a, b, calls = _setup(monkeypatch)
    out = asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "merge", "keep_id": str(a.id),
        "reason": "unsure", "confidence": CONFIDENCE_GATE - 0.001,
    }], FakeUser()))

    assert out["merged"] == 0 and out["annotated"] == 1
    assert calls == []
    assert s.status == "pending"
    assert "unsure" in s.rationale               # reasoning still recorded


def test_the_gate_boundary_is_inclusive(monkeypatch):
    db, s, a, b, calls = _setup(monkeypatch)
    asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "merge", "keep_id": str(a.id),
        "reason": "ok", "confidence": CONFIDENCE_GATE,
    }], FakeUser()))
    assert len(calls) == 1


def test_distinct_dismisses_without_merging(monkeypatch):
    db, s, a, b, calls = _setup(monkeypatch)
    out = asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "distinct",
        "reason": "different email addresses", "confidence": 0.97,
    }], FakeUser()))

    assert out["dismissed"] == 1 and out["merged"] == 0
    assert calls == []
    assert s.status == "rejected"


def test_unclear_leaves_the_pair_for_a_human(monkeypatch):
    db, s, a, b, calls = _setup(monkeypatch)
    out = asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "unclear",
        "reason": "would need the underlying documents", "confidence": 0.99,
    }], FakeUser()))

    # High confidence in "unclear" must never be read as confidence to merge.
    assert out["annotated"] == 1 and out["merged"] == 0 and out["dismissed"] == 0
    assert s.status == "pending"
    assert "would need the underlying documents" in s.rationale


def test_co_occurrence_does_not_block_a_merge(monkeypatch):
    # Regression guard for the original design error. OCR puts the correct
    # spelling and its corruption in the SAME document, so a pair sharing
    # documents is a normal duplicate and must still merge.
    db, s, a, b, calls = _setup(monkeypatch)
    asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "merge", "keep_id": str(a.id),
        "reason": "both spellings appear in SCHLEGEL 004102; v/w OCR confusion",
        "confidence": 0.94,
    }], FakeUser()))
    assert len(calls) == 1, "a co-occurring pair must still be mergeable"


def test_a_pair_a_human_already_resolved_is_skipped(monkeypatch):
    db, s, a, b, calls = _setup(monkeypatch, status="accepted")
    out = asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "merge", "keep_id": str(a.id),
        "reason": "x", "confidence": 0.99,
    }], FakeUser()))

    assert out["skipped"] == 1 and out["merged"] == 0
    assert calls == []                            # the human's decision stands


def test_a_suggestion_from_another_production_is_skipped(monkeypatch):
    db, s, a, b, calls = _setup(monkeypatch)
    out = asyncio.run(apply_verdicts(db, 999, [{
        "suggestion_id": 1, "verdict": "merge", "keep_id": str(a.id),
        "reason": "x", "confidence": 0.99,
    }], FakeUser()))
    assert out["skipped"] == 1 and calls == []


def test_keeper_defaults_to_the_more_mentioned_side(monkeypatch):
    # No keep_id from the model: the dominant spelling should survive, since
    # the minority spelling is usually the corruption.
    db, s, a, b, calls = _setup(monkeypatch, mention_a=2, mention_b=40)
    asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "merge", "keep_id": None,
        "reason": "ok", "confidence": 0.9,
    }], FakeUser()))
    winner, loser, _ = calls[0]
    assert winner is b and loser is a


def test_a_rejected_merge_is_counted_not_raised(monkeypatch):
    db, s, a, b, calls = _setup(monkeypatch)

    async def boom(db_, winner, loser, user_id):
        raise ValueError("Entities are of different types")

    monkeypatch.setattr(mr, "merge_entities", boom)
    out = asyncio.run(apply_verdicts(db, 1, [{
        "suggestion_id": 1, "verdict": "merge", "keep_id": str(a.id),
        "reason": "x", "confidence": 0.99,
    }], FakeUser()))

    assert out["merged"] == 0 and out["skipped"] == 1
    assert any("different types" in r for r in out["skip_reasons"])
