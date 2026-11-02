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
    """doc_rows: (id, control, override, image_paths, source_type, source_party); red_rows: (id, count, changed)."""
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
    db = _db([(d1, "C-1", None, ["p1.jpg"], None, None)])
    out = _run(db, [d1])
    assert out["total"] == 0
    assert out == {"qc_pending": [], "privilege_produce": [],
                   "no_images": [], "received_document": [], "needs_pages": [],
                   "redactions_overridden": [], "attachment_names_redacted": [], "total": 0}


def test_redactions_without_approval_conflict():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"], None, None)], red_rows=[(d1, 2, TS)])
    out = _run(db, [d1])
    assert out["total"] == 1
    assert out["qc_pending"][0]["control_number"] == "C-1"
    assert "pending" in out["qc_pending"][0]["detail"]


def test_fresh_approved_qc_no_conflict():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"], None, None)],
             red_rows=[(d1, 2, TS)],
             decisions=[FakeDecision(d1, "approved", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert out["total"] == 0


def test_stale_approval_conflicts():
    # redaction changed AFTER the decision -> auto-invalidated -> pending
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"], None, None)],
             red_rows=[(d1, 2, TS + timedelta(hours=2))],
             decisions=[FakeDecision(d1, "approved", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert len(out["qc_pending"]) == 1


def test_rejected_qc_conflicts():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"], None, None)],
             red_rows=[(d1, 2, TS)],
             decisions=[FakeDecision(d1, "rejected", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert "rejected" in out["qc_pending"][0]["detail"]


def test_privilege_produce_override_conflicts():
    d1 = uuid4()
    db = _db([(d1, "C-1", "produce", ["p1.jpg"], None, None)], privileged=[d1])
    out = _run(db, [d1])
    assert len(out["privilege_produce"]) == 1
    assert out["total"] == 1


def test_privileged_withhold_is_fine():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, [], None, None)], privileged=[d1])  # withhold, no images OK
    out = _run(db, [d1])
    assert out["total"] == 0


def test_no_images_conflict_for_produce_doc():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, [], None, None)])
    out = _run(db, [d1])
    assert len(out["no_images"]) == 1


def test_empty_doc_ids():
    out = _run(FakeSession(), [])
    assert out["total"] == 0


def test_received_document_conflict():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"], "received", "ABC Corp")])
    out = _run(db, [d1])
    assert len(out["received_document"]) == 1
    assert "ABC Corp" in out["received_document"][0]["detail"]
    assert out["total"] == 1


def test_null_source_type_not_flagged():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"], None, None)])
    out = _run(db, [d1])
    assert out["received_document"] == []


def _db2(doc_rows, info=(), parents=(), parent_reds=(), red_rows=()):
    """info: (id, file_type, native_path, imaging_status, family_id, header_regions)."""
    return FakeSession(responders=[
        ("documents.image_paths", FakeResult(rows=list(doc_rows))),
        ("is_privilege", FakeResult(rows=[])),
        ("coalesce", FakeResult(rows=list(red_rows))),
        ("redaction_qc_decisions", FakeResult(items=[])),
        ("documents.imaging_status", FakeResult(rows=list(info))),
        ("documents.header_regions", FakeResult(rows=list(parents))),
        ("redactions.reason_code", FakeResult(items=list(parent_reds))),
    ])


def test_imageable_email_without_pages_needs_pages_not_no_images():
    d1 = uuid4()
    out = _run(_db2([(d1, "C-1", None, [], None, None)],
                    info=[(d1, "email", None, None, "C-1", None)]), [d1])
    assert [c["control_number"] for c in out["needs_pages"]] == ["C-1"]
    assert out["no_images"] == []


def test_office_file_stays_no_images():
    d1 = uuid4()
    out = _run(_db2([(d1, "C-1 .0001", None, [], None, None)],
                    info=[(d1, "xlsx", "productions/1/attachments/x/b.xlsx", None, "C-1", None)]), [d1])
    assert out["needs_pages"] == [] and len(out["no_images"]) == 1
    assert "can't be imaged" in out["no_images"][0]["detail"]


def test_queued_imaging_needs_pages():
    d1 = uuid4()
    out = _run(_db2([(d1, "C-1", None, [], None, None)],
                    info=[(d1, "email", None, "running", "C-1", None)]), [d1])
    assert len(out["needs_pages"]) == 1


def test_redactions_with_produce_override_conflict():
    d1 = uuid4()
    out = _run(_db2([(d1, "C-1", "produce", ["p.jpg"], None, None)], red_rows=[(d1, 1, TS)]), [d1])
    assert [c["control_number"] for c in out["redactions_overridden"]] == ["C-1"]


def test_attachment_names_redacted_on_parent_conflicts_for_child():
    parent, child = uuid4(), uuid4()
    regions = {"attachments": {"page": 1, "x": 10.0, "y": 17.0, "w": 80.0, "h": 2.0}}
    red = type("R", (), dict(document_id=parent, page_num=1, x_pct=20.0, y_pct=17.5, w_pct=30.0, h_pct=1.0))
    out = _run(_db2([(child, "C-1 .0001", None, ["p.jpg"], None, None)],
                    info=[(child, "pdf", "productions/1/attachments/x/a.pdf", None, "C-1", None)],
                    parents=[(parent, "C-1", regions)], parent_reds=[red]), [child])
    assert [c["control_number"] for c in out["attachment_names_redacted"]] == ["C-1 .0001"]


def test_attachment_without_redacted_parent_line_is_fine():
    parent, child = uuid4(), uuid4()
    regions = {"attachments": {"page": 1, "x": 10.0, "y": 17.0, "w": 80.0, "h": 2.0}}
    out = _run(_db2([(child, "C-1 .0001", None, ["p.jpg"], None, None)],
                    info=[(child, "pdf", "productions/1/attachments/x/a.pdf", None, "C-1", None)],
                    parents=[(parent, "C-1", regions)], parent_reds=[]), [child])
    assert out["attachment_names_redacted"] == []
