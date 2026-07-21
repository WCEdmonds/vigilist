"""Fake-session unit tests for redaction CRUD endpoints (P1-1). No DB/network.

Same pattern as tests/test_review_endpoints.py: call the async router functions
directly with a fake session + monkeypatched deps.
"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.redactions as rr
from app.schemas import RedactionCreate, RedactionUpdate

_TS = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"
        self.display_name = uid


class FakeDoc:
    def __init__(self, doc_id, production_id=1, page_count=10):
        self.id = doc_id
        self.production_id = production_id
        self.page_count = page_count


class FakeRedaction:
    def __init__(self, rid, document_id, created_by, page_num=1):
        self.id = rid
        self.document_id = document_id
        self.created_by = created_by
        self.page_num = page_num
        self.x_pct = 10.0
        self.y_pct = 10.0
        self.w_pct = 10.0
        self.h_pct = 10.0
        self.reason_code = "pii"
        self.note = None
        self.created_at = _TS
        self.updated_at = None


class FakeSession:
    def __init__(self, get_objects=None):
        self._get_objects = get_objects or {}
        self.added = []
        self.deleted = []

    async def get(self, model, key):
        return self._get_objects.get((model.__name__, key))

    def add(self, obj):
        obj.id = 123
        if getattr(obj, "created_at", None) is None:
            obj.created_at = _TS  # DB server_default isn't applied in-memory
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "created_at", None) is None:
            obj.created_at = _TS

    async def delete(self, obj):
        self.deleted.append(obj)


def _patch_common(monkeypatch, role="reviewer", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        pass

    monkeypatch.setattr(rr, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rr, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rr, "log_action", fake_log)


def test_create_blocked_for_readonly(monkeypatch):
    _patch_common(monkeypatch, role="readonly")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionCreate(page_num=1, x_pct=10, y_pct=10, w_pct=10, h_pct=10, reason_code="pii")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.create_redaction(doc_id=doc_id, body=body, db=db, user=FakeUser("u1")))
    assert exc.value.status_code == 403


def test_create_rejects_invalid_reason_code(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionCreate(page_num=1, x_pct=10, y_pct=10, w_pct=10, h_pct=10, reason_code="bogus")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.create_redaction(doc_id=doc_id, body=body, db=db, user=FakeUser("u1")))
    assert exc.value.status_code == 422


def test_create_rejects_box_exceeding_page(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionCreate(page_num=1, x_pct=80, y_pct=10, w_pct=30, h_pct=10, reason_code="pii")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.create_redaction(doc_id=doc_id, body=body, db=db, user=FakeUser("u1")))
    assert exc.value.status_code == 422


def test_create_succeeds_for_reviewer(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionCreate(page_num=2, x_pct=10, y_pct=10, w_pct=20, h_pct=20, reason_code="attorney_client", note="privileged")
    out = asyncio.run(rr.create_redaction(doc_id=doc_id, body=body, db=db, user=FakeUser("u1")))
    assert out.reason_code == "attorney_client"
    assert out.page_num == 2
    assert len(db.added) == 1


def test_update_blocked_for_noncreator_reviewer(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    red = FakeRedaction(5, doc_id, created_by="owner")
    db = FakeSession(get_objects={("Redaction", 5): red, ("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionUpdate(reason_code="pii")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.update_redaction(redaction_id=5, body=body, db=db, user=FakeUser("someone_else")))
    assert exc.value.status_code == 403


def test_update_allowed_for_manager(monkeypatch):
    _patch_common(monkeypatch, role="manager")
    doc_id = uuid4()
    red = FakeRedaction(5, doc_id, created_by="owner")
    db = FakeSession(get_objects={("Redaction", 5): red, ("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionUpdate(reason_code="confidential")
    out = asyncio.run(rr.update_redaction(redaction_id=5, body=body, db=db, user=FakeUser("someone_else")))
    assert out.reason_code == "confidential"


def test_delete_allowed_for_creator(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    red = FakeRedaction(7, doc_id, created_by="u1")
    db = FakeSession(get_objects={("Redaction", 7): red, ("Document", doc_id): FakeDoc(doc_id)})
    out = asyncio.run(rr.delete_redaction(redaction_id=7, db=db, user=FakeUser("u1")))
    assert out == {"ok": True}
    assert db.deleted == [red]
