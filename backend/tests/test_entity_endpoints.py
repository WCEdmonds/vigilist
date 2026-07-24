"""Fake-session tests for the entities read API: scoping + shapes."""

import asyncio
import uuid
from datetime import date

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity, OntologyEvent
from app.schemas import EventEditRequest
from tests.fakes import FakeResult, FakeSession, FakeUser


ENT_ID = uuid.uuid4()
EVENT_ID = 42


def _entity(production_id=1):
    return Entity(id=ENT_ID, production_id=production_id, entity_type="person",
                  canonical_name="Jorge Rivera", aliases=["J. Rivera"], attributes={},
                  overview="Existing overview", overview_mention_count=100, mention_count=100)


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)


def test_get_entity_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))  # entity is in production 1
    db = FakeSession(get_objects={("Entity", ENT_ID): _entity(production_id=1)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_entity(entity_id=ENT_ID, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_get_entity_returns_profile_without_regenerating_fresh_overview(monkeypatch):
    _patch(monkeypatch)
    called = {"gen": False}

    async def fake_generate(db, entity):
        called["gen"] = True
        return "new overview"
    monkeypatch.setattr(er, "generate_entity_overview", fake_generate)
    db = FakeSession(
        get_objects={("Entity", ENT_ID): _entity()},
        responders=[("count", FakeResult(scalar=7))],
    )
    out = asyncio.run(er.get_entity(entity_id=ENT_ID, db=db, user=FakeUser()))
    assert out.canonical_name == "Jorge Rivera"
    assert out.overview == "Existing overview"
    assert called["gen"] is False  # fresh — no regeneration


def test_document_entities_denies_unknown_document(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession()  # no document
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_document_entities(doc_id=uuid.uuid4(), db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_list_production_entities_denies_out_of_scope_production(monkeypatch):
    _patch(monkeypatch, accessible=(2,))  # production 1 not accessible
    db = FakeSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.list_production_entities(production_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_get_entity_mentions_denies_out_of_scope_entity(monkeypatch):
    _patch(monkeypatch, accessible=(2,))  # entity is in production 1
    db = FakeSession(get_objects={("Entity", ENT_ID): _entity(production_id=1)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_entity_mentions(entity_id=ENT_ID, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_get_entity_connections_denies_out_of_scope_entity(monkeypatch):
    _patch(monkeypatch, accessible=(2,))  # entity is in production 1
    db = FakeSession(get_objects={("Entity", ENT_ID): _entity(production_id=1)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_entity_connections(entity_id=ENT_ID, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


# ── PATCH / DELETE /api/events/{event_id} ──────────────────────────────────

def _event(production_id=1, d=None, precision="unknown"):
    ev = OntologyEvent(production_id=production_id, event_type="meeting",
                       description="Board meeting", event_date=d,
                       date_precision=precision, document_id=uuid.uuid4(),
                       significance=4, date_source_text="minutes dated 2021")
    ev.id = EVENT_ID
    return ev


def _patch_event(monkeypatch, accessible=(1,), role="manager"):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(er, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(er, "log_action", fake_log)


def _event_db(ev):
    return FakeSession(get_objects={("OntologyEvent", EVENT_ID): ev})


def test_patch_updates_date_and_precision(monkeypatch):
    _patch_event(monkeypatch)
    ev = _event(d=None, precision="unknown")
    db = _event_db(ev)
    out = asyncio.run(er.edit_event(
        event_id=EVENT_ID, body=EventEditRequest(event_date="2021-06-15"),
        db=db, user=FakeUser()))
    assert ev.event_date == date(2021, 6, 15)
    assert ev.date_precision == "day"
    assert out["event_date"] == "2021-06-15" and out["date_precision"] == "day"


def test_patch_clears_date_on_null(monkeypatch):
    _patch_event(monkeypatch)
    ev = _event(d=date(2021, 6, 15), precision="day")
    db = _event_db(ev)
    out = asyncio.run(er.edit_event(
        event_id=EVENT_ID, body=EventEditRequest(event_date=None),
        db=db, user=FakeUser()))
    assert ev.event_date is None
    assert ev.date_precision == "unknown"
    assert out["event_date"] is None and out["date_precision"] == "unknown"


def test_patch_rejects_bad_precision(monkeypatch):
    _patch_event(monkeypatch)
    db = _event_db(_event())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.edit_event(
            event_id=EVENT_ID, body=EventEditRequest(date_precision="decade"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_patch_denies_out_of_scope(monkeypatch):
    _patch_event(monkeypatch, accessible=(2,))  # event is in production 1
    db = _event_db(_event(production_id=1))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.edit_event(
            event_id=EVENT_ID, body=EventEditRequest(date_precision="day"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_patch_manager_gate(monkeypatch):
    _patch_event(monkeypatch, role="readonly")
    db = _event_db(_event())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.edit_event(
            event_id=EVENT_ID, body=EventEditRequest(date_precision="day"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_delete_removes_event(monkeypatch):
    _patch_event(monkeypatch)
    ev = _event()
    db = _event_db(ev)
    out = asyncio.run(er.delete_event(event_id=EVENT_ID, db=db, user=FakeUser()))
    assert out == {"ok": True}
    assert ev in db.deleted


def test_delete_denies_out_of_scope(monkeypatch):
    _patch_event(monkeypatch, accessible=(2,))  # event is in production 1
    db = _event_db(_event(production_id=1))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.delete_event(event_id=EVENT_ID, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_delete_manager_gate(monkeypatch):
    _patch_event(monkeypatch, role="readonly")
    db = _event_db(_event())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.delete_event(event_id=EVENT_ID, db=db, user=FakeUser()))
    assert exc.value.status_code == 403
