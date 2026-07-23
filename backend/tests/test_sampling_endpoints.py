"""Fake-session tests for sampling endpoints (P3-2). No DB."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.sampling as rsm
from app.schemas import SampleCreate
from tests.fakes import FakeResult, FakeSession, FakeUser


class FakeSample:
    def __init__(self, sample_id=1, production_id=1, **kw):
        self.id = sample_id
        self.production_id = production_id
        self.name = kw.get("name", "Richness sample")
        self.purpose = kw.get("purpose", "richness")
        self.params = kw.get("params", {"population": 1000, "confidence": 95})
        self.document_ids = kw.get("document_ids", [])
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

    monkeypatch.setattr(rsm, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rsm, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rsm, "log_action", fake_log)
    return logged


# --- calculator -------------------------------------------------------------

def test_calculator():
    out = asyncio.run(rsm.calculate_sample_size(
        population=100_000, confidence=95, margin=0.05, expected_rate=0.5,
        user=FakeUser()))
    assert out["sample_size"] == 383


def test_calculator_validation():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rsm.calculate_sample_size(
            population=100, confidence=80, margin=0.05, expected_rate=0.5,
            user=FakeUser()))
    assert exc.value.status_code == 422


# --- draw -------------------------------------------------------------------

def test_draw_computes_size_and_freezes_ids(monkeypatch):
    logged = _patch(monkeypatch)
    ids = [(uuid4(),) for _ in range(10)]
    db = FakeSession(responders=[
        ("random", FakeResult(rows=ids)),
        ("count", FakeResult(scalar=500)),
    ])
    out = asyncio.run(rsm.draw_sample(
        production_id=1, body=SampleCreate(name="S1"), db=db, user=FakeUser()))
    assert out.params["population"] == 500
    assert out.params["size"] == 10
    assert len(out.document_ids) == 10
    assert all(isinstance(d, str) for d in out.document_ids)
    assert "sample_drawn" in logged


def test_draw_empty_population_422(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[("count", FakeResult(scalar=0))])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rsm.draw_sample(
            production_id=1, body=SampleCreate(name="S1"), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_draw_bad_purpose_422(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rsm.draw_sample(
            production_id=1, body=SampleCreate(name="S1", purpose="vibes"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 422


def test_draw_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rsm.draw_sample(
            production_id=1, body=SampleCreate(name="S1"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


# --- estimate / acceptance --------------------------------------------------

def test_estimate_counts_tag_intersection(monkeypatch):
    _patch(monkeypatch)
    sample_ids = [str(uuid4()) for _ in range(100)]
    tagged = [(sample_ids[i],) for i in range(10)] + [(str(uuid4()),)]  # 1 outside
    smp = FakeSample(document_ids=sample_ids,
                     params={"population": 10_000, "confidence": 95})
    db = FakeSession(
        get_objects={("Sample", 1): smp},
        responders=[("document_tags", FakeResult(rows=tagged))],
    )
    out = asyncio.run(rsm.sample_estimate(sample_id=1, tag_id=7, confidence=None,
                                          db=db, user=FakeUser()))
    assert out["n"] == 100
    assert out["positives"] == 10          # the outside doc doesn't count
    assert out["rate"] == 0.10
    assert 0 < out["ci_low"] < 0.10 < out["ci_high"] < 1
    assert out["estimated_low"] == int(out["ci_low"] * 10_000)


def test_acceptance_verdict(monkeypatch):
    _patch(monkeypatch)
    sample_ids = [str(uuid4()) for _ in range(200)]
    smp = FakeSample(document_ids=sample_ids)
    db = FakeSession(
        get_objects={("Sample", 1): smp},
        responders=[("document_tags", FakeResult(rows=[]))],  # zero defects
    )
    out = asyncio.run(rsm.sample_acceptance(sample_id=1, tag_id=7, tolerable=0.05,
                                            confidence=None, db=db, user=FakeUser()))
    assert out["accept"] is True
    assert out["n"] == 200


def test_delete_audits(monkeypatch):
    logged = _patch(monkeypatch)
    db = FakeSession(get_objects={("Sample", 1): FakeSample()})
    out = asyncio.run(rsm.delete_sample(sample_id=1, db=db, user=FakeUser()))
    assert out == {"ok": True}
    assert "sample_deleted" in logged


# --- machine_negative scope (P3-3) ------------------------------------------

def test_draw_machine_negative_requires_project(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rsm.draw_sample(
            production_id=1,
            body=SampleCreate(name="E1", purpose="elusion", scope="machine_negative"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 422


def test_draw_machine_negative_scopes_to_null_set(monkeypatch):
    _patch(monkeypatch)
    ids = [(uuid4(),) for _ in range(5)]
    db = FakeSession(responders=[
        ("random", FakeResult(rows=ids)),
        ("count", FakeResult(scalar=50)),
    ])
    out = asyncio.run(rsm.draw_sample(
        production_id=1,
        body=SampleCreate(name="E1", purpose="elusion",
                          scope="machine_negative", project_id=3),
        db=db, user=FakeUser()))
    assert out.params["scope"] == "machine_negative"
    assert out.params["project_id"] == 3
    joined = "\n".join(db.executed)
    assert "ai_review_results" in joined
