"""Fake-session tests for redaction QC endpoints (P1-4)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.redactions as rr
from app.schemas import RedactionQCDecisionCreate
from tests.fakes import TS, FakeResult, FakeSession, FakeUser


class FakeDoc:
    def __init__(self, doc_id, production_id=1, page_count=10):
        self.id = doc_id
        self.production_id = production_id
        self.page_count = page_count


class FakeDecision:
    def __init__(self, document_id, decision="approved", decided_at=None,
                 redaction_count=2, dec_id=5):
        self.id = dec_id
        self.document_id = document_id
        self.decision = decision
        self.note = None
        self.redaction_count = redaction_count
        self.decided_by = "u1"
        self.decided_at = decided_at or TS


def _patch(monkeypatch, role="manager", accessible=(1,)):
    audit_calls = []

    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(db, user, action, resource_type, resource_id, **kwargs):
        audit_calls.append((action, resource_type, resource_id, kwargs))

    monkeypatch.setattr(rr, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rr, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rr, "log_action", fake_log)
    return audit_calls


# --- POST /documents/{id}/redaction-qc ------------------------------------

def test_qc_decision_created_with_count_snapshot(monkeypatch):
    audit_calls = _patch(monkeypatch, role="manager")
    doc_id = uuid4()
    db = FakeSession(
        get_objects={("Document", doc_id): FakeDoc(doc_id)},
        responders=[("count(redactions.id", FakeResult(scalar=3))],
    )
    body = RedactionQCDecisionCreate(decision="approved", note="looks right")
    out = asyncio.run(rr.decide_redaction_qc(doc_id=doc_id, body=body, db=db, user=FakeUser()))
    assert out.decision == "approved"
    assert out.redaction_count == 3
    assert len(db.added) == 1
    assert audit_calls == [("redaction_qc_decided", "redaction_qc", "1000",
                            {"production_id": 1,
                             "details": {"document_id": str(doc_id), "decision": "approved",
                                         "redaction_count": 3}})]


def test_qc_decision_422_when_no_redactions(monkeypatch):
    _patch(monkeypatch, role="manager")
    doc_id = uuid4()
    db = FakeSession(
        get_objects={("Document", doc_id): FakeDoc(doc_id)},
        responders=[("count(redactions.id", FakeResult(scalar=0))],
    )
    body = RedactionQCDecisionCreate(decision="approved")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.decide_redaction_qc(doc_id=doc_id, body=body, db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_qc_decision_blocked_for_reviewer(monkeypatch):
    audit_calls = _patch(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionQCDecisionCreate(decision="approved")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.decide_redaction_qc(doc_id=doc_id, body=body, db=db, user=FakeUser()))
    assert exc.value.status_code == 403
    assert audit_calls == []


def test_qc_decision_rejects_invalid_decision_value():
    with pytest.raises(Exception):
        RedactionQCDecisionCreate(decision="maybe")


# --- GET /productions/{id}/redaction-qc -----------------------------------

def test_qc_queue_computes_statuses(monkeypatch):
    _patch(monkeypatch)
    d1, d2, d3 = uuid4(), uuid4(), uuid4()
    changed_late = TS + timedelta(hours=2)
    agg_rows = [
        (d1, "DOC-001", 2, TS),            # fresh approval below -> approved
        (d2, "DOC-002", 2, changed_late),  # edited after decision -> pending
        (d3, "DOC-003", 1, TS),            # no decision -> pending
    ]
    decisions = [
        FakeDecision(d1, "approved", TS + timedelta(hours=1), 2, dec_id=1),
        FakeDecision(d2, "approved", TS + timedelta(hours=1), 2, dec_id=2),
    ]
    db = FakeSession(responders=[
        ("JOIN redactions", FakeResult(rows=agg_rows)),
        ("FROM redaction_qc_decisions", FakeResult(items=decisions)),
    ])
    out = asyncio.run(rr.redaction_qc_queue(production_id=1, db=db, user=FakeUser()))
    by_bates = {i.bates_begin: i for i in out}
    assert by_bates["DOC-001"].qc_status == "approved"
    assert by_bates["DOC-002"].qc_status == "pending"
    assert by_bates["DOC-003"].qc_status == "pending"
    assert by_bates["DOC-001"].latest_decision.decision == "approved"
    assert by_bates["DOC-003"].latest_decision is None


def test_qc_queue_403_outside_accessible_productions(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    db = FakeSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.redaction_qc_queue(production_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 403
