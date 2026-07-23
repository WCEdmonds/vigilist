"""Fake-session tests for production-set endpoints (P2-1)."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.production_sets as rps
from app.schemas import ProductionSetCreate
from tests.fakes import TS, FakeResult, FakeSession, FakeUser


class FakePS:
    def __init__(self, set_id=1, production_id=1, status="draft", **kw):
        self.id = set_id
        self.production_id = production_id
        self.name = kw.get("name", "Vol 1")
        self.status = status
        self.prefix = kw.get("prefix", "SMITH")
        self.padding = kw.get("padding", 6)
        self.start_number = kw.get("start_number", 1)
        self.sort_key = kw.get("sort_key", "control_number")
        self.designation = kw.get("designation", None)
        self.created_by = "u1"
        self.created_at = TS
        self.locked_by = None
        self.locked_at = None


class FakeItem:
    def __init__(self, document_id, **kw):
        self.id = kw.get("item_id", None)
        self.document_id = document_id
        self.sort_order = kw.get("sort_order", None)
        self.bates_begin = kw.get("bates_begin", None)
        self.bates_end = kw.get("bates_end", None)
        self.pages = kw.get("pages", None)
        self.disposition = kw.get("disposition", None)
        self.designation = kw.get("designation", None)


def _patch(monkeypatch, role="manager", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        pass

    monkeypatch.setattr(rps, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rps, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rps, "log_action", fake_log)


# --- POST /productions/{id}/production-sets --------------------------------

def test_create_draft_set(monkeypatch):
    _patch(monkeypatch, role="manager")
    db = FakeSession()
    out = asyncio.run(rps.create_production_set(
        production_id=1,
        body=ProductionSetCreate(name="Vol 1", prefix="SMITH"),
        db=db, user=FakeUser()))
    assert out.status == "draft"
    assert out.prefix == "SMITH"
    assert out.padding == 6
    assert out.doc_count == 0
    assert len(db.added) == 1


def test_create_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1, body=ProductionSetCreate(name="V", prefix="P"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_create_403_outside_accessible(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1, body=ProductionSetCreate(name="V", prefix="P"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_create_rejects_whitespace_prefix(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1, body=ProductionSetCreate(name="V", prefix="SMITH VOL"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 422


def test_create_rejects_unknown_sort_key(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1,
            body=ProductionSetCreate(name="V", prefix="P", sort_key="bogus"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 422


def test_create_duplicate_name_409(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[("FROM production_sets", FakeResult(scalar=7))])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1, body=ProductionSetCreate(name="Vol 1", prefix="P"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 409


# --- GET list / detail ------------------------------------------------------

def test_list_sets_with_doc_counts(monkeypatch):
    _patch(monkeypatch)
    s1, s2 = FakePS(set_id=1), FakePS(set_id=2, name="Vol 2", status="locked")
    db = FakeSession(responders=[
        ("FROM production_set_items", FakeResult(rows=[(1, 3)])),
        ("FROM production_sets", FakeResult(items=[s1, s2])),
    ])
    out = asyncio.run(rps.list_production_sets(production_id=1, db=db, user=FakeUser()))
    assert [o.doc_count for o in out] == [3, 0]


def test_detail_locked_set_aggregates(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    ps = FakePS(status="locked")
    items = [
        FakeItem(d1, item_id=1, sort_order=1, bates_begin="SMITH000001",
                 bates_end="SMITH000003", pages=3, disposition="produce"),
        FakeItem(d2, item_id=2, sort_order=2, bates_begin="SMITH000004",
                 bates_end="SMITH000004", pages=1, disposition="withhold"),
    ]
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("FROM production_set_items", FakeResult(items=items))],
    )
    out = asyncio.run(rps.get_production_set(set_id=1, db=db, user=FakeUser()))
    assert out.doc_count == 2
    assert out.page_count == 4
    assert out.bates_begin == "SMITH000001"
    assert out.bates_end == "SMITH000004"


def test_detail_404(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.get_production_set(set_id=9, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


# --- GET members ------------------------------------------------------------

def test_members_list_maps_rows(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    ps = FakePS()
    item = FakeItem(d1, item_id=1)
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("JOIN documents", FakeResult(rows=[(item, "C-001")]))],
    )
    out = asyncio.run(rps.list_production_set_documents(set_id=1, db=db, user=FakeUser()))
    assert len(out) == 1
    assert out[0].document_id == d1
    assert out[0].control_number == "C-001"
    assert out[0].bates_begin is None  # draft: not yet assigned


# --- DELETE set -------------------------------------------------------------

def test_delete_draft_set(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS()
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    out = asyncio.run(rps.delete_production_set(set_id=1, db=db, user=FakeUser()))
    assert out == {"ok": True}


def test_delete_locked_set_409(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.delete_production_set(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 409
