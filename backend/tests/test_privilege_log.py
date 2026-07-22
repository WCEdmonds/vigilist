"""Fake-session tests for privilege overrides + privilege log (P1-5)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.privilege as rp
from app.schemas import PrivilegeOverrideUpdate
from app.services.privilege_log import build_privilege_log_rows
from tests.fakes import TS, FakeResult, FakeSession, FakeUser


class FakeDoc:
    def __init__(self, doc_id, production_id=1, **kw):
        self.id = doc_id
        self.production_id = production_id
        self.bates_begin = kw.get("bates_begin", "DOC-001")
        self.bates_end = kw.get("bates_end", "DOC-002")
        self.custodian = kw.get("custodian", "T. Owner")
        self.date_sent = kw.get("date_sent", TS)
        self.date_received = kw.get("date_received", None)
        self.email_from = kw.get("email_from", "alice@firm.com")
        self.email_to = kw.get("email_to", "bob@client.com")
        self.file_type = kw.get("file_type", "eml")
        self.privilege_disposition = kw.get("privilege_disposition", None)
        self.privilege_description = kw.get("privilege_description", None)


class FakeRedactionRow:
    def __init__(self, document_id, reason_code="pii", created_at=None, updated_at=None):
        self.document_id = document_id
        self.reason_code = reason_code
        self.created_at = created_at or TS
        self.updated_at = updated_at


class FakeDecision:
    def __init__(self, document_id, decision="approved", decided_at=None, redaction_count=1):
        self.document_id = document_id
        self.decision = decision
        self.decided_at = decided_at or (TS + timedelta(hours=1))
        self.redaction_count = redaction_count


def _log_db(tagged_rows, redactions, docs, decisions=(), override_docs=()):
    """tagged_rows: (doc_id, tag_name) tuples; docs: list[FakeDoc]."""
    return FakeSession(responders=[
        ("JOIN tags", FakeResult(rows=list(tagged_rows))),
        ("FROM redactions", FakeResult(items=list(redactions))),
        ("FROM redaction_qc_decisions", FakeResult(items=list(decisions))),
        ("privilege_disposition IS NOT NULL", FakeResult(items=list(override_docs))),
        ("FROM documents", FakeResult(items=list(docs))),
    ])


def test_log_withhold_row_for_privilege_tagged_doc():
    doc_id = uuid4()
    doc = FakeDoc(doc_id)
    db = _log_db([(doc_id, "Attorney-Client")], [], [doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert len(rows) == 1
    r = rows[0]
    assert r["disposition"] == "withhold"
    assert r["basis"] == ["Attorney-Client"]
    assert r["qc_status"] == "not_applicable"
    assert r["description"] == ("Email from alice@firm.com to bob@client.com "
                                "dated 2026-07-22 withheld on the basis of Attorney-Client.")


def test_log_redact_in_part_merges_tag_and_reason_basis():
    doc_id = uuid4()
    doc = FakeDoc(doc_id)
    db = _log_db(
        [(doc_id, "Attorney-Client")],
        [FakeRedactionRow(doc_id, "pii"), FakeRedactionRow(doc_id, "attorney_client")],
        [doc],
    )
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    r = rows[0]
    assert r["disposition"] == "redact_in_part"
    assert r["basis"] == ["ATTORNEY-CLIENT", "Attorney-Client", "PII"]  # deduped, sorted
    assert r["qc_status"] == "pending"


def test_log_redactions_only_doc_included_with_reason_basis():
    doc_id = uuid4()
    doc = FakeDoc(doc_id)
    db = _log_db([], [FakeRedactionRow(doc_id, "trade_secret")], [doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    r = rows[0]
    assert r["disposition"] == "redact_in_part"
    assert r["basis"] == ["TRADE SECRET"]


def test_log_produce_override_excluded():
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_disposition="produce")
    db = _log_db([(doc_id, "Attorney-Client")], [], [doc], override_docs=[doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert rows == []


def test_log_override_doc_without_tag_or_redactions_included():
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_disposition="withhold")
    db = _log_db([], [], [doc], override_docs=[doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert len(rows) == 1
    assert rows[0]["disposition"] == "withhold"


def test_log_manual_description_wins():
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_description="Letter re legal advice.")
    db = _log_db([(doc_id, "Attorney-Client")], [], [doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert rows[0]["description"] == "Letter re legal advice."


def test_log_qc_approved_reflected():
    doc_id = uuid4()
    doc = FakeDoc(doc_id)
    db = _log_db(
        [(doc_id, "Attorney-Client")],
        [FakeRedactionRow(doc_id, "pii")],
        [doc],
        decisions=[FakeDecision(doc_id, "approved", TS + timedelta(hours=1), 1)],
    )
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert rows[0]["qc_status"] == "approved"


# --- PUT /documents/{id}/privilege ----------------------------------------

def _patch(monkeypatch, role="manager", accessible=(1,)):
    audit_calls = []

    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(db, user, action, resource_type, resource_id, **kwargs):
        audit_calls.append((action, resource_type, resource_id, kwargs))

    monkeypatch.setattr(rp, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rp, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rp, "log_action", fake_log)
    return audit_calls


def test_override_set_and_clear(monkeypatch):
    audit_calls = _patch(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_disposition="withhold")
    db = FakeSession(get_objects={("Document", doc_id): doc})
    out = asyncio.run(rp.update_privilege(
        doc_id=doc_id,
        body=PrivilegeOverrideUpdate(disposition=None, description="X."),
        db=db, user=FakeUser()))
    assert doc.privilege_disposition is None      # explicit null cleared it
    assert doc.privilege_description == "X."
    assert out["disposition"] is None
    assert out["description"] == "X."
    assert audit_calls == [("privilege_override_set", "document", str(doc_id),
                            {"production_id": 1,
                             "details": {"disposition": None, "has_description": True}})]


def test_override_omitted_field_untouched(monkeypatch):
    _patch(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_disposition="withhold")
    db = FakeSession(get_objects={("Document", doc_id): doc})
    asyncio.run(rp.update_privilege(
        doc_id=doc_id, body=PrivilegeOverrideUpdate(description="Y."),
        db=db, user=FakeUser()))
    assert doc.privilege_disposition == "withhold"  # not in fields_set -> untouched


def test_override_invalid_disposition_422(monkeypatch):
    _patch(monkeypatch)
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rp.update_privilege(
            doc_id=doc_id, body=PrivilegeOverrideUpdate(disposition="bogus"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_override_blocked_for_reviewer(monkeypatch):
    audit_calls = _patch(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rp.update_privilege(
            doc_id=doc_id, body=PrivilegeOverrideUpdate(disposition="withhold"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 403
    assert audit_calls == []


# --- CSV export ------------------------------------------------------------

def test_privilege_log_csv_shape(monkeypatch):
    import app.routers.export as re_

    async def fake_accessible(db, user):
        return [1]

    monkeypatch.setattr(re_, "get_accessible_production_ids", fake_accessible)

    async def fake_rows(db, production_id):
        return [{
            "document_id": "x", "bates_begin": "DOC-001", "bates_end": "DOC-002",
            "doc_date": "2026-07-22", "custodian": "T. Owner",
            "author": "alice@firm.com", "recipients": "bob@client.com",
            "file_type": "eml", "disposition": "withhold",
            "basis": ["Attorney-Client", "PII"],
            "description": "Email from alice@firm.com dated 2026-07-22 withheld.",
            "qc_status": "not_applicable",
        }]

    monkeypatch.setattr(re_, "build_privilege_log_rows", fake_rows)
    out = asyncio.run(re_.export_privilege_log_csv(production_id=1, db=FakeSession(), user=FakeUser()))
    text = out.body.decode()
    lines = text.strip().splitlines()
    assert lines[0] == ("Bates Begin,Bates End,Date,Custodian,Author,Recipients,"
                       "Doc Type,Disposition,Privilege Basis,Description,Redaction QC")
    assert "DOC-001" in lines[1]
    assert "Attorney-Client; PII" in lines[1]
    assert "privilege_log.csv" in out.headers["content-disposition"]
