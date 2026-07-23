"""Fake-session tests for production validation conflicts (P2-3.5)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import app.services.production_validation as pv
from tests.fakes import TS, FakeResult, FakeSession


class FakePS:
    def __init__(self):
        self.id = 1
        self.production_id = 1


class FakeDecision:
    def __init__(self, document_id, decision="approved", decided_at=None,
                 redaction_count=1, dec_id=1):
        self.id = dec_id
        self.document_id = document_id
        self.decision = decision
        self.decided_at = decided_at or (TS + timedelta(hours=1))
        self.redaction_count = redaction_count


def _db(doc_rows, privileged=(), red_rows=(), decisions=()):
    """doc_rows: (id, control, override, image_paths); red_rows: (id, count, changed)."""
    return FakeSession(responders=[
        ("documents.image_paths", FakeResult(rows=list(doc_rows))),
        ("is_privilege", FakeResult(rows=[(d,) for d in privileged])),
        ("coalesce", FakeResult(rows=list(red_rows))),
        ("redaction_qc_decisions", FakeResult(items=list(decisions))),
    ])


def _run(db, doc_ids):
    return asyncio.run(pv.compute_conflicts(db, FakePS(), doc_ids))


def test_clean_set_no_conflicts():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])])
    out = _run(db, [d1])
    assert out["total"] == 0
    assert out == {"qc_pending": [], "privilege_produce": [],
                   "no_images": [], "total": 0}


def test_redactions_without_approval_conflict():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])], red_rows=[(d1, 2, TS)])
    out = _run(db, [d1])
    assert out["total"] == 1
    assert out["qc_pending"][0]["control_number"] == "C-1"
    assert "pending" in out["qc_pending"][0]["detail"]


def test_fresh_approved_qc_no_conflict():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])],
             red_rows=[(d1, 2, TS)],
             decisions=[FakeDecision(d1, "approved", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert out["total"] == 0


def test_stale_approval_conflicts():
    # redaction changed AFTER the decision -> auto-invalidated -> pending
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])],
             red_rows=[(d1, 2, TS + timedelta(hours=2))],
             decisions=[FakeDecision(d1, "approved", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert len(out["qc_pending"]) == 1


def test_rejected_qc_conflicts():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])],
             red_rows=[(d1, 2, TS)],
             decisions=[FakeDecision(d1, "rejected", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert "rejected" in out["qc_pending"][0]["detail"]


def test_privilege_produce_override_conflicts():
    d1 = uuid4()
    db = _db([(d1, "C-1", "produce", ["p1.jpg"])], privileged=[d1])
    out = _run(db, [d1])
    assert len(out["privilege_produce"]) == 1
    assert out["total"] == 1


def test_privileged_withhold_is_fine():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, [])], privileged=[d1])  # withhold, no images OK
    out = _run(db, [d1])
    assert out["total"] == 0


def test_no_images_conflict_for_produce_doc():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, [])])
    out = _run(db, [d1])
    assert len(out["no_images"]) == 1


def test_empty_doc_ids():
    out = _run(FakeSession(), [])
    assert out["total"] == 0
