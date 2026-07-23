"""Fake-session tests for the entities read API: scoping + shapes."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity
from tests.fakes import FakeResult, FakeSession, FakeUser


ENT_ID = uuid.uuid4()


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
