import asyncio
import uuid

import pytest

import app.routers.productions as pr
from app.models import Entity, Production
from app.services.brief import resolve_key_players
from tests.fakes import FakeResult, FakeSession, FakeUser


def _ent(name, aliases=None):
    return Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                  canonical_name=name, aliases=aliases or [], attributes={}, mention_count=1)


def test_resolves_by_normalized_name_and_alias():
    jorge = _ent("Jorge Rivera")
    acme = _ent("Acme Corp Inc", aliases=["Acme"])
    db = FakeSession(responders=[("FROM entities", FakeResult(items=[jorge, acme]))])
    out = asyncio.run(resolve_key_players(db, 1, ["jorge rivera", "Acme", "Nobody Known"]))
    assert out[0] == {"name": "jorge rivera", "entity_id": str(jorge.id)}
    assert out[1] == {"name": "Acme", "entity_id": str(acme.id)}
    assert out[2] == {"name": "Nobody Known", "entity_id": None}


def test_empty_names_short_circuits():
    assert asyncio.run(resolve_key_players(FakeSession(), 1, [])) == []


# ── Endpoint-level tests for get_pipeline brief augmentation ──


def test_get_pipeline_augments_key_players_with_resolved_entity_ids(monkeypatch):
    """Brief with key_players + resolvable entity → response.key_players_resolved has entity_id."""
    entity_id = uuid.uuid4()
    prod = Production(
        id=1, name="Test", owner_id="owner1", ai_pipeline_status={},
        brief={"overview": "Test", "key_players": ["Jorge Rivera"]},
        case_context=None
    )

    # Mock get_user_role_for_production to allow access
    async def mock_role(db, user, prod_id):
        return "admin"
    monkeypatch.setattr(pr, "get_user_role_for_production", mock_role)

    # Mock resolve_key_players to return valid data with an entity_id
    async def mock_resolve(db, prod_id, names):
        return [{"name": "Jorge Rivera", "entity_id": str(entity_id)}]
    monkeypatch.setattr("app.services.brief.resolve_key_players", mock_resolve)

    db = FakeSession(
        get_objects={("Production", 1): prod},
        responders=[
            ("count", FakeResult(rows=[(1, 0)])),  # doc_count, summarized_count
        ]
    )

    out = asyncio.run(pr.get_pipeline(1, db, FakeUser()))
    assert out.key_players_resolved is not None
    assert len(out.key_players_resolved) == 1
    assert out.key_players_resolved[0].name == "Jorge Rivera"
    assert str(out.key_players_resolved[0].entity_id) == str(entity_id)


def test_get_pipeline_handles_resolve_key_players_exception(monkeypatch):
    """resolve_key_players raising → endpoint still returns, key_players_resolved is None."""
    prod = Production(
        id=1, name="Test", owner_id="owner1", ai_pipeline_status={},
        brief={"overview": "Test", "key_players": ["Jorge Rivera"]},
        case_context=None
    )

    # Mock access control
    async def mock_role(db, user, prod_id):
        return "admin"
    monkeypatch.setattr(pr, "get_user_role_for_production", mock_role)

    # Mock resolve_key_players to raise
    async def mock_resolve(db, prod_id, names):
        raise RuntimeError("Database connection failed")
    monkeypatch.setattr("app.services.brief.resolve_key_players", mock_resolve)

    db = FakeSession(
        get_objects={("Production", 1): prod},
        responders=[
            ("count", FakeResult(rows=[(1, 0)])),
        ]
    )

    out = asyncio.run(pr.get_pipeline(1, db, FakeUser()))
    assert out.key_players_resolved is None  # degraded gracefully


def test_get_pipeline_handles_malformed_entity_id_in_resolved_list(monkeypatch):
    """Malformed entity_id from helper → endpoint still returns 200, key_players_resolved is None."""
    prod = Production(
        id=1, name="Test", owner_id="owner1", ai_pipeline_status={},
        brief={"overview": "Test", "key_players": ["Unknown Person"]},
        case_context=None
    )

    # Mock access control
    async def mock_role(db, user, prod_id):
        return "admin"
    monkeypatch.setattr(pr, "get_user_role_for_production", mock_role)

    # Mock resolve_key_players to return malformed entity_id (not a valid UUID)
    async def mock_resolve(db, prod_id, names):
        return [{"name": "Unknown Person", "entity_id": "not-a-uuid"}]
    monkeypatch.setattr("app.services.brief.resolve_key_players", mock_resolve)

    db = FakeSession(
        get_objects={("Production", 1): prod},
        responders=[
            ("count", FakeResult(rows=[(1, 0)])),
        ]
    )

    out = asyncio.run(pr.get_pipeline(1, db, FakeUser()))
    # The malformed UUID is caught in try/except, degrading to None instead of 500
    assert out.key_players_resolved is None
