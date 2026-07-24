"""Fake-session tests for lineage / exceptions / chain-of-custody (P3-4)."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.defensibility as rd
from tests.fakes import TS, FakeResult, FakeSession, FakeUser


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    monkeypatch.setattr(rd, "get_accessible_production_ids", fake_accessible)


class FakeDoc:
    def __init__(self, doc_id):
        self.id = doc_id
        self.production_id = 1
        self.bates_begin = "C-1"
        self.source_party = "ABC Corp"
        self.source_type = "received"
        self.source_path = "loads/x/mail.eml"
        self.file_name = "mail.eml"
        self.file_type = "email"
        self.custodian = "T. Owner"
        self.file_hash_md5 = "md5x"
        self.file_hash_sha256 = "shax"
        self.extraction_status = "ok"
        self.extraction_error = None


class FakeReviewRow:
    def __init__(self):
        self.project_id = 3
        self.ai_decision = "relevant"
        self.confidence_score = 88
        self.attorney_decision = None
        self.created_at = TS


class FakeQC:
    def __init__(self):
        self.decision = "approved"
        self.decided_by = "u1"
        self.decided_at = TS


class FakeItem:
    def __init__(self):
        self.bates_begin = "SMITH000001"
        self.bates_end = "SMITH000002"
        self.disposition = "produce"
        self.produce_native = False
        self.output_path = "productions/1/production_sets/1/SMITH000001.pdf"


class FakeAudit:
    def __init__(self, action):
        self.action = action
        self.user_email = "u1@thirulaw.com"
        self.created_at = TS
        self.details = {}


def test_lineage_assembles_all_sections(monkeypatch):
    _patch(monkeypatch)
    doc_id = uuid4()
    db = FakeSession(
        get_objects={("Document", str(doc_id)): FakeDoc(doc_id)},
        responders=[
            ("redaction_qc_decisions", FakeResult(items=[FakeQC()])),
            ("count(redactions", FakeResult(scalar=2)),
            ("document_tags", FakeResult(rows=[("Responsive", "u1", TS)])),
            ("ai_review_results", FakeResult(items=[FakeReviewRow()])),
            ("production_set_items", FakeResult(rows=[(FakeItem(), "Vol 1", "locked")])),
            ("audit_logs", FakeResult(items=[FakeAudit("tag_applied")])),
        ],
    )
    out = asyncio.run(rd.document_lineage(doc_id=str(doc_id), db=db, user=FakeUser()))
    assert out["identity"]["sha256"] == "shax"
    assert out["identity"]["source_party"] == "ABC Corp"
    assert out["tags"][0]["name"] == "Responsive"
    assert out["review"][0]["ai_decision"] == "relevant"
    assert out["redactions"]["count"] == 2
    assert out["redactions"]["qc_decisions"][0]["decision"] == "approved"
    assert out["productions"][0]["bates_begin"] == "SMITH000001"
    assert out["audit"][0]["action"] == "tag_applied"


def test_lineage_403_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", str(doc_id)): FakeDoc(doc_id)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rd.document_lineage(doc_id=str(doc_id), db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_exceptions_report_counts_and_rows(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    rows = [(d1, "C-1", "bad.zip", "ABC Corp", "error", "corrupt archive"),
            (d2, "C-2", "locked.pdf", None, "encrypted", "password required")]
    db = FakeSession(responders=[("extraction_status", FakeResult(rows=rows))])
    out = asyncio.run(rd.exceptions_report(production_id=1, db=db, user=FakeUser()))
    assert out["total"] == 2
    assert out["counts"] == {"error": 1, "encrypted": 1}
    assert out["exceptions"][0]["control_number"] == "C-1"


def test_exceptions_csv_shape(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    rows = [(d1, "C-1", "bad.zip", "ABC Corp", "error", "corrupt archive")]
    db = FakeSession(responders=[("extraction_status", FakeResult(rows=rows))])
    out = asyncio.run(rd.exceptions_csv(production_id=1, db=db, user=FakeUser()))
    lines = out.body.decode().strip().splitlines()
    assert lines[0] == "Control Number,File Name,Source Party,Status,Error"
    assert lines[1] == "C-1,bad.zip,ABC Corp,error,corrupt archive"


class FakeJob:
    def __init__(self):
        self.id = uuid4()
        self.source_format = "native"
        self.status = "complete"
        self.total_files = 10
        self.processed_files = 9
        self.skipped_files = 1
        self.field_mapping = {"source_party": "Our Collection", "source_type": "collection"}
        self.created_at = TS
        self.completed_at = TS


class FakeProject:
    def __init__(self):
        self.id = 3
        self.name = "Responsiveness"
        self.status = "complete"
        self.processed_documents = 900
        self.total_documents = 1000


class FakeValidation:
    def __init__(self):
        self.id = 12
        self.project_id = 3
        self.created_at = TS
        self.results = {"control": {"recall": {"rate": 0.87},
                                    "precision": {"rate": 0.91}},
                        "elusion": {"rate": 0.004}}


class FakeSet:
    def __init__(self):
        self.id = 1
        self.name = "Vol 1"
        self.status = "locked"
        self.prefix = "SMITH"
        self.render_status = "rendered"
        self.package_status = "packaged"
        self.packaged_at = TS
        self.conflicts_overridden_by = None


def test_chain_of_custody_aggregates(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[
        ("ingest_jobs", FakeResult(items=[FakeJob()])),
        ("GROUP BY documents.source_type", FakeResult(rows=[("received", 800), (None, 200)])),
        ("GROUP BY documents.extraction_status", FakeResult(rows=[("ok", 995), ("error", 5)])),
        ("file_hash_sha256", FakeResult(scalar=990)),
        ("review_projects", FakeResult(items=[FakeProject()])),
        ("tar_validation_reports", FakeResult(items=[FakeValidation()])),
        ("production_sets", FakeResult(items=[FakeSet()])),
        ("count", FakeResult(scalar=1000)),
    ])
    out = asyncio.run(rd.chain_of_custody(production_id=1, db=db, user=FakeUser()))
    assert out["loads"][0]["source_party"] == "Our Collection"
    assert out["documents"]["total"] == 1000
    assert out["documents"]["hashed_sha256"] == 990
    assert out["documents"]["by_source_type"]["received"] == 800
    assert out["review"][0]["validation"]["recall"] == 0.87
    assert out["review"][0]["validation"]["elusion_rate"] == 0.004
    assert out["productions"][0]["package_status"] == "packaged"
