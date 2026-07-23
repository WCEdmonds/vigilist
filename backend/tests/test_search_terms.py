"""Fake-session tests for search-term hit reports (P3-1). No DB."""

import asyncio
from uuid import uuid4

import app.services.search_terms as st
from tests.fakes import FakeResult, FakeSession


def _db(total, fam_rows, per_term_results):
    """per_term_results: list of rows-lists, served to '@@' queries in order."""
    queue = list(per_term_results)

    def next_term_result(sql):
        return FakeResult(rows=queue.pop(0)) if queue else FakeResult()

    return FakeSession(responders=[
        ("@@", next_term_result),
        ("family_id IS NOT NULL", FakeResult(rows=fam_rows)),
        ("count", FakeResult(scalar=total)),
    ])


def _run(db, terms, source_type=None):
    return asyncio.run(st.run_search_term_report(db, 1, terms, source_type))


def test_hits_uniqueness_and_totals():
    d1, d2, d3 = uuid4(), uuid4(), uuid4()
    db = _db(
        total=10,
        fam_rows=[],
        per_term_results=[
            [(d1, None), (d2, None)],   # "alpha" hits d1, d2
            [(d2, None), (d3, None)],   # "beta" hits d2, d3
        ],
    )
    out = _run(db, ["alpha", "beta"])
    assert out["total_docs"] == 10
    assert out["any_hits"] == 3
    a, b = out["terms"]
    assert (a["term"], a["hits"], a["unique_hits"]) == ("alpha", 2, 1)  # d1 unique
    assert (b["term"], b["hits"], b["unique_hits"]) == ("beta", 2, 1)   # d3 unique


def test_family_expansion_pulls_non_hit_siblings():
    d1, sibling = uuid4(), uuid4()
    db = _db(
        total=5,
        fam_rows=[(d1, "F1"), (sibling, "F1")],
        per_term_results=[[(d1, "F1")]],
    )
    out = _run(db, ["alpha"])
    row = out["terms"][0]
    assert row["hits"] == 1
    assert row["with_families"] == 2      # sibling rides along
    assert out["any_with_families"] == 2


def test_empty_sanitized_term_scores_zero():
    db = _db(total=5, fam_rows=[], per_term_results=[])
    out = _run(db, ["***"])
    row = out["terms"][0]
    assert (row["hits"], row["with_families"], row["unique_hits"]) == (0, 0, 0)


def test_source_type_threads_into_scope():
    db = _db(total=3, fam_rows=[], per_term_results=[[]])
    _run(db, ["alpha"], source_type="collection")
    joined = "\n".join(db.executed)
    assert "IS DISTINCT FROM" in joined
    db2 = _db(total=3, fam_rows=[], per_term_results=[[]])
    _run(db2, ["alpha"], source_type="received")
    assert "IS DISTINCT FROM" not in "\n".join(db2.executed)
