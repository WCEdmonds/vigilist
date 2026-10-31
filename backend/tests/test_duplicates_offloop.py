"""Duplicate detection must not hold the event loop or an open transaction
through its CPU work (MinHash over every document + all-pairs similarity)."""

import asyncio
import time

import numpy as np

import app.services.duplicates as dup
from tests.fakes import FakeResult, FakeSession


def _naive_similar_pairs(ids, M):
    """The original pair loop, kept as the oracle."""
    pairs = []
    for start in range(0, len(ids), 500):
        sims = M[start:start + 500] @ M.T
        for i, row in enumerate(sims):
            gi = start + i
            for j in range(gi + 1, len(ids)):
                s = float(row[j])
                if 0.80 <= s < 0.95:
                    pairs.append((ids[gi], ids[j], s))
    return pairs


def test_similar_pairs_match_original_loop():
    rng = np.random.default_rng(0)
    base = rng.normal(size=(30, 16))
    # near-copies so the 0.80-0.95 band is populated
    M = np.vstack([base, base + rng.normal(scale=0.35, size=base.shape)])
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    ids = [f"d{i}" for i in range(len(M))]
    got = dup._similar_pairs(ids, M)
    want = _naive_similar_pairs(ids, M)
    assert len(want) > 0
    assert sorted((a, b) for a, b, _ in got) == sorted((a, b) for a, b, _ in want)
    by_pair = {(a, b): s for a, b, s in want}
    assert all(abs(by_pair[(a, b)] - s) < 1e-9 for a, b, s in got)


def test_compute_groups_finds_exact_and_similar():
    text = "the quick brown fox jumps over the lazy dog again and again"
    docs = [("a", text), ("b", text), ("c", "completely different words appear in this one here")]
    emb = {"a": [1.0, 0.0], "b": [1.0, 0.0], "c": [0.9, 0.45]}
    hash_c, exact_c, similar_c = dup._compute_groups([("a", "h"), ("c", "h")], docs, emb)
    assert hash_c == [["a", "c"]]
    assert [sorted(d for d, _ in g) for g in exact_c] == [["a", "b"]]
    # c is ~0.89 cosine to a and b (in the 0.80-0.95 band); a~b is 1.0 (excluded)
    assert [sorted(d for d, _ in g) for g in similar_c] == [["a", "b", "c"]]


class _OrderedSession(FakeSession):
    def __init__(self, events, **kw):
        super().__init__(**kw)
        self.events = events

    async def execute(self, stmt, params=None):
        self.events.append(str(stmt).split()[0])
        return await super().execute(stmt, params)

    async def commit(self):
        self.events.append("COMMIT")
        await super().commit()


def _patch(monkeypatch, events, delay=0.0):
    async def fake_embeddings(db, production_id):
        return {"a": [1.0, 0.0], "b": [1.0, 0.0]}

    real = dup._compute_groups

    def slow(hash_rows, docs, embeddings):
        time.sleep(delay)
        events.append("compute")
        return real(hash_rows, docs, embeddings)

    monkeypatch.setattr(dup, "get_document_embeddings", fake_embeddings)
    monkeypatch.setattr(dup, "_compute_groups", slow)


def _session(events):
    text = "one two three four five six seven eight"
    return _OrderedSession(events, responders=[
        ("FROM duplicate_groups", FakeResult(rows=[(9,)])),
        ("file_hash_sha256", FakeResult(rows=[("a", "h"), ("b", "h")])),
        ("text_content", FakeResult(rows=[("a", text), ("b", text)])),
    ])


def test_detect_duplicates_keeps_event_loop_free(monkeypatch):
    events = []
    _patch(monkeypatch, events, delay=0.5)
    db = _session(events)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while "compute" not in events:
            ticks += 1
            await asyncio.sleep(0.02)

    async def main():
        await asyncio.gather(dup.detect_duplicates(db, 1), ticker())

    asyncio.run(main())
    assert ticks >= 10


def test_old_groups_replaced_only_after_compute(monkeypatch):
    events = []
    _patch(monkeypatch, events)
    db = _session(events)
    out = asyncio.run(dup.detect_duplicates(db, 1))
    compute_at = events.index("compute")
    first_delete = events.index("DELETE")
    assert compute_at < first_delete
    # reads done and the connection released before the CPU work
    assert "COMMIT" in events[:compute_at]
    assert out["hash_groups"] == 1 and out["exact_groups"] == 1
