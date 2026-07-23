"""Fake-session tests for TAR validation (P3-3). No DB."""

import asyncio

from app.services.tar_validation import build_validation
from tests.fakes import FakeResult, FakeSession


class FakeSample:
    def __init__(self, sample_id, document_ids):
        self.id = sample_id
        self.document_ids = document_ids


def _db(decision_rows, tag_queues, null_count=0):
    """tag_queues: list of row-lists served to document_tags queries in order."""
    queue = list(tag_queues)

    def next_tags(sql):
        return FakeResult(rows=queue.pop(0)) if queue else FakeResult()

    return FakeSession(responders=[
        ("count", FakeResult(scalar=null_count)),
        ("document_tags", next_tags),
        ("ai_review_results", FakeResult(rows=decision_rows)),
    ])


def test_full_confusion_matrix_scenario():
    control = FakeSample(1, [f"d{i}" for i in range(8)])
    decisions = [("d0", "relevant"), ("d1", "key_document"), ("d2", "not_relevant"),
                 ("d3", "relevant"), ("d4", "not_relevant"), ("d5", "needs_review")]
    human_pos = [("d0",), ("d1",), ("d2",), ("d5",)]
    human_neg = [("d3",), ("d4",)]
    db = _db(decisions, [human_pos, human_neg])
    out = asyncio.run(build_validation(
        db, 1, project_id=3, control_sample=control,
        responsive_tag_id=7, nonresponsive_tag_id=8,
        elusion_sample=None, confidence=95))
    c = out["control"]
    assert c["n"] == 8
    assert c["coded"] == 6
    assert c["uncoded"] == 2                      # d6, d7
    assert c["machine_undecided"] == 1            # d5 needs_review
    assert c["confusion"] == {"tp": 2, "fp": 1, "fn": 1, "tn": 1}
    assert abs(c["recall"]["rate"] - 2 / 3) < 1e-9
    assert abs(c["precision"]["rate"] - 2 / 3) < 1e-9
    assert abs(c["richness"]["rate"] - 4 / 6) < 1e-9
    assert c["recall"]["low"] < 2 / 3 < c["recall"]["high"]
    assert out["elusion"] is None
    assert any("uncoded" in n for n in c["notes"])


def test_conflicted_docs_excluded_and_reported():
    control = FakeSample(1, ["d0", "d1"])
    db = _db([("d0", "relevant"), ("d1", "relevant")],
             [[("d0",), ("d1",)], [("d1",)]])   # d1 carries both tags
    out = asyncio.run(build_validation(
        db, 1, 3, control, 7, 8, None, 95))
    c = out["control"]
    assert c["conflicted"] == 1
    assert c["coded"] == 1
    assert c["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 0}
    assert any("both tags" in n for n in c["notes"])


def test_recall_undefined_when_no_human_positives():
    control = FakeSample(1, ["d0"])
    db = _db([("d0", "relevant")], [[], [("d0",)]])
    out = asyncio.run(build_validation(db, 1, 3, control, 7, 8, None, 95))
    c = out["control"]
    assert c["recall"] is None
    assert c["precision"] is not None             # fp = 1
    assert any("recall undefined" in n for n in c["notes"])


def test_elusion_math_and_extrapolation():
    control = FakeSample(1, ["d0"])
    elusion = FakeSample(2, [f"e{i}" for i in range(100)])
    db = _db(
        [("d0", "relevant")],
        [[("d0",)], [], [("e0",), ("e1",)]],      # control pos, control neg, elusion pos
        null_count=10_000,
    )
    out = asyncio.run(build_validation(db, 1, 3, control, 7, 8, elusion, 95))
    e = out["elusion"]
    assert e["n"] == 100
    assert e["positives"] == 2
    assert e["rate"] == 0.02
    assert e["null_set_size"] == 10_000
    assert e["estimated_missed_low"] == int(e["low"] * 10_000)
    assert e["estimated_missed_high"] == int(e["high"] * 10_000)
    assert e["estimated_missed_low"] < 200 < e["estimated_missed_high"]


# --- endpoints ---------------------------------------------------------------

import pytest
from fastapi import HTTPException

import app.routers.tar as rt
from app.schemas import TarValidationCreate
from tests.fakes import FakeUser


class FakeProject:
    def __init__(self, project_id=3, production_id=1):
        self.id = project_id
        self.production_id = production_id


class FakePersistedSample:
    def __init__(self, sample_id, purpose, production_id=1):
        self.id = sample_id
        self.production_id = production_id
        self.purpose = purpose
        self.document_ids = ["d0"]


def _patch(monkeypatch, role="manager", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    logged = []

    async def fake_log(db, user, action, *a, **kw):
        logged.append(action)

    async def fake_build(db, production_id, project_id, control, resp, nonresp,
                         elusion, confidence):
        return {"confidence": confidence, "project_id": project_id,
                "control": {"recall": {"rate": 0.9}},
                "elusion": None, "generated_at": "now"}

    monkeypatch.setattr(rt, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rt, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rt, "log_action", fake_log)
    monkeypatch.setattr(rt, "build_validation", fake_build)
    return logged


def test_run_validation_persists_and_audits(monkeypatch):
    logged = _patch(monkeypatch)
    db = FakeSession(get_objects={
        ("ReviewProject", 3): FakeProject(),
        ("Sample", 10): FakePersistedSample(10, "control"),
    })
    out = asyncio.run(rt.run_validation(
        production_id=1,
        body=TarValidationCreate(project_id=3, control_sample_id=10,
                                 responsive_tag_id=7),
        db=db, user=FakeUser()))
    assert out.results["control"]["recall"]["rate"] == 0.9
    assert len(db.added) == 1
    assert "tar_validation_run" in logged


def test_run_validation_rejects_wrong_purpose(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={
        ("ReviewProject", 3): FakeProject(),
        ("Sample", 10): FakePersistedSample(10, "richness"),
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.run_validation(
            production_id=1,
            body=TarValidationCreate(project_id=3, control_sample_id=10,
                                     responsive_tag_id=7),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_run_validation_rejects_foreign_project(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={
        ("ReviewProject", 3): FakeProject(production_id=2),
        ("Sample", 10): FakePersistedSample(10, "control"),
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.run_validation(
            production_id=1,
            body=TarValidationCreate(project_id=3, control_sample_id=10,
                                     responsive_tag_id=7),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_run_validation_rejects_elusion_purpose_mismatch(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={
        ("ReviewProject", 3): FakeProject(),
        ("Sample", 10): FakePersistedSample(10, "control"),
        ("Sample", 11): FakePersistedSample(11, "richness"),
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.run_validation(
            production_id=1,
            body=TarValidationCreate(project_id=3, control_sample_id=10,
                                     responsive_tag_id=7, elusion_sample_id=11),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_run_validation_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.run_validation(
            production_id=1,
            body=TarValidationCreate(project_id=3, control_sample_id=10,
                                     responsive_tag_id=7),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403
