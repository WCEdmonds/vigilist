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


# --- endpoints ---------------------------------------------------------------

import pytest
from fastapi import HTTPException

import app.routers.search_terms as rst
from app.schemas import SearchTermReportCreate
from tests.fakes import FakeUser


class FakeReport:
    def __init__(self, report_id=1, production_id=1, **kw):
        self.id = report_id
        self.production_id = production_id
        self.name = kw.get("name", "Negotiated terms v1")
        self.terms = kw.get("terms", ["alpha", "beta"])
        self.results = kw.get("results", None)
        self.computed_at = None
        self.created_by = "u1"
        self.created_at = None


def _patch(monkeypatch, role="manager", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    logged = []

    async def fake_log(db, user, action, *a, **kw):
        logged.append(action)

    monkeypatch.setattr(rst, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rst, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rst, "log_action", fake_log)
    return logged


def test_create_report(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession()
    out = asyncio.run(rst.create_report(
        production_id=1,
        body=SearchTermReportCreate(name=" Terms v1 ", terms=[" alpha ", "beta"]),
        db=db, user=FakeUser()))
    assert out.name == "Terms v1"
    assert out.terms == ["alpha", "beta"]
    assert len(db.added) == 1


def test_create_report_validation(monkeypatch):
    _patch(monkeypatch)
    for body in (SearchTermReportCreate(name="  ", terms=["a"]),
                 SearchTermReportCreate(name="X", terms=[]),
                 SearchTermReportCreate(name="X", terms=["ok", "  "])):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(rst.create_report(production_id=1, body=body,
                                          db=FakeSession(), user=FakeUser()))
        assert exc.value.status_code == 422


def test_create_report_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rst.create_report(
            production_id=1, body=SearchTermReportCreate(name="X", terms=["a"]),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_run_persists_results_and_audits(monkeypatch):
    logged = _patch(monkeypatch)

    async def fake_run(db, production_id, terms, source_type=None):
        return {"total_docs": 5, "any_hits": 2, "any_with_families": 3,
                "source_type": source_type,
                "terms": [{"term": "alpha", "hits": 2, "with_families": 3, "unique_hits": 2}],
                "computed_at": "now"}

    monkeypatch.setattr(rst, "run_search_term_report", fake_run)
    rpt = FakeReport()
    db = FakeSession(get_objects={("SearchTermReport", 1): rpt})
    out = asyncio.run(rst.run_report(report_id=1, body=None, db=db, user=FakeUser()))
    assert out["any_hits"] == 2
    assert rpt.results == out
    assert rpt.computed_at is not None
    assert "search_term_report_run" in logged


def test_run_rejects_bad_source_type(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("SearchTermReport", 1): FakeReport()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rst.run_report(report_id=1, body={"source_type": "sideways"},
                                   db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_csv_shape_and_total_row(monkeypatch):
    _patch(monkeypatch)
    rpt = FakeReport(results={
        "total_docs": 10, "any_hits": 3, "any_with_families": 4,
        "terms": [
            {"term": "alpha", "hits": 2, "with_families": 3, "unique_hits": 1},
            {"term": "beta", "hits": 2, "with_families": 2, "unique_hits": 1},
        ]})
    db = FakeSession(get_objects={("SearchTermReport", 1): rpt})
    out = asyncio.run(rst.export_report_csv(report_id=1, db=db, user=FakeUser()))
    lines = out.body.decode().strip().splitlines()
    assert lines[0] == "Term,Documents with hits,Docs + families,Unique hits"
    assert lines[1].startswith("alpha,2,3,1")
    assert lines[-1] == "TOTAL (any term),3,4,2"


def test_csv_404_before_run(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("SearchTermReport", 1): FakeReport()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rst.export_report_csv(report_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_delete_manager_only(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("SearchTermReport", 1): FakeReport()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rst.delete_report(report_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 403
