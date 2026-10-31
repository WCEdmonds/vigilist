"""Clustering must be fast, numerically faithful, and never hold the event loop.

Incident 2026-10-29..31: the k-selection loop (pure-Python O(n^2) silhouette,
26 values of k, 2,838 docs) ran on the event loop for most of an hour, froze
every request on its instance, and its DB connection was dropped mid-stage so
clustering failed and re-ran on every retry.
"""

import asyncio
import time
from uuid import uuid4

import numpy as np

import app.services.clustering as cl
from tests.fakes import FakeResult, FakeSession


def _reference_silhouette(X, labels):
    """The original implementation, kept as the oracle."""
    n = len(X)
    unique_labels = set(labels)
    scores = []
    for i in range(n):
        same = [j for j in range(n) if labels[j] == labels[i] and j != i]
        if not same:
            continue
        a = np.mean([np.linalg.norm(X[i] - X[j]) for j in same])
        b = float("inf")
        for label in unique_labels:
            if label == labels[i]:
                continue
            others = [j for j in range(n) if labels[j] == label]
            if others:
                b = min(b, np.mean([np.linalg.norm(X[i] - X[j]) for j in others]))
        if b == float("inf"):
            continue
        scores.append((b - a) / max(a, b))
    return float(np.mean(scores)) if scores else 0.0


def _blobs(n_per=40, d=16, centers=3, seed=0):
    rng = np.random.default_rng(seed)
    C = rng.normal(size=(centers, d)) * 10
    X = np.vstack([C[c] + rng.normal(size=(n_per, d)) for c in range(centers)]).astype(np.float32)
    truth = np.repeat(np.arange(centers), n_per)
    return X / np.linalg.norm(X, axis=1, keepdims=True), truth


def test_silhouette_matches_reference():
    X, _ = _blobs(n_per=30, centers=4)
    labels = np.random.default_rng(1).integers(0, 4, size=len(X))
    labels[0] = 7  # a singleton cluster is skipped, as before
    assert abs(cl._silhouette_score(X, labels) - _reference_silhouette(X, labels)) < 1e-4


def test_silhouette_matches_reference_on_kmeans_labels():
    X, _ = _blobs(n_per=40, centers=3)
    labels = cl._kmeans(X, 3)
    assert abs(cl._silhouette_score(X, labels) - _reference_silhouette(X, labels)) < 1e-4


def test_silhouette_degenerate_inputs():
    X, _ = _blobs(n_per=5, centers=1)
    assert cl._silhouette_score(X, np.zeros(len(X), dtype=int)) == 0.0
    assert cl._silhouette_score(X[:2], np.array([0, 1])) == 0.0


def test_kmeans_recovers_separated_blobs():
    X, truth = _blobs(n_per=50, centers=3)
    labels = cl._kmeans(X, 3)
    for c in range(3):
        assert len(set(labels[truth == c])) == 1
    assert len(set(labels)) == 3


def test_choose_clusters_picks_true_k():
    X, _ = _blobs(n_per=60, centers=6, d=32)
    labels, k, score = cl.choose_clusters(X)
    assert k == 6 and len(set(labels)) == 6 and score > 0.5


def test_choose_clusters_fast_at_production_scale():
    # 2,838 x 1024 took most of an hour before; must now be seconds.
    rng = np.random.default_rng(0)
    X = rng.normal(size=(3000, 1024)).astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    t0 = time.perf_counter()
    labels, k, _ = cl.choose_clusters(X)
    assert time.perf_counter() - t0 < 60
    assert labels.shape == (3000,) and 5 <= k <= 30


def _patch_pipeline_io(monkeypatch, events, n=12, compute_delay=0.0):
    ids = [uuid4() for _ in range(n)]
    X, _ = _blobs(n_per=n // 3, centers=3, d=8)

    async def fake_embeddings(db, production_id):
        return {d: X[i].tolist() for i, d in enumerate(ids)}

    real_choose = cl.choose_clusters

    def slow_choose(X, num_clusters=None):
        time.sleep(compute_delay)  # stands in for heavy CPU work
        events.append("compute")
        return real_choose(X, num_clusters)

    async def fake_label(titles, lines):
        events.append("label")
        return "Topic"

    monkeypatch.setattr(cl, "get_document_embeddings", fake_embeddings)
    monkeypatch.setattr(cl, "choose_clusters", slow_choose)
    monkeypatch.setattr(cl, "_generate_cluster_label", fake_label)
    return ids


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


def test_cluster_production_keeps_event_loop_free(monkeypatch):
    events = []
    _patch_pipeline_io(monkeypatch, events, compute_delay=0.5)
    db = _OrderedSession(events, responders=[("FROM document_clusters", FakeResult(rows=[]))])
    ticks = 0

    async def ticker():
        nonlocal ticks
        while "compute" not in events:
            ticks += 1
            await asyncio.sleep(0.02)

    async def main():
        await asyncio.gather(cl.cluster_production(db, 1), ticker())

    asyncio.run(main())
    # 0.5s of CPU work on the loop would allow ~1 tick; off-loop allows ~25.
    assert ticks >= 10


def test_old_clusters_replaced_only_after_new_ones_are_ready(monkeypatch):
    events = []
    _patch_pipeline_io(monkeypatch, events)
    db = _OrderedSession(events, responders=[
        ("FROM document_clusters", FakeResult(rows=[(1,), (2,)])),
    ])
    asyncio.run(cl.cluster_production(db, 1, num_clusters=3))
    first_delete = events.index("DELETE")
    assert events.index("compute") < first_delete
    # Reads finish and the connection is released before the long CPU/LLM work.
    compute_at = events.index("compute")
    assert "COMMIT" in events[:compute_at]
    assert all(e not in ("SELECT", "DELETE", "INSERT") for e in events[events.index("COMMIT"):compute_at])
    assert max(i for i, e in enumerate(events) if e == "label") < first_delete
