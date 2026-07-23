import asyncio
import uuid

from app.models import Entity
from app.services.brief import resolve_key_players
from tests.fakes import FakeResult, FakeSession


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
