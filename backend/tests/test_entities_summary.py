import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity
from tests.fakes import FakeResult, FakeSession, FakeUser

D1, D2 = uuid.uuid4(), uuid.uuid4()


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)


def _ent(name, etype="person"):
    return Entity(id=uuid.uuid4(), production_id=1, entity_type=etype,
                  canonical_name=name, aliases=[], attributes={}, mention_count=1)


def test_summary_groups_top3_per_doc(monkeypatch):
    _patch(monkeypatch)
    ents = [_ent(f"P{i}") for i in range(4)]
    rows = [(D1, ents[i], 10 - i) for i in range(4)] + [(D2, ents[0], 2)]
    db = FakeSession(responders=[("FROM entity_mentions", FakeResult(rows=rows))])
    out = asyncio.run(er.get_entities_summary(ids=f"{D1},{D2}", db=db, user=FakeUser()))
    assert len(out.summaries[str(D1)]) == 3          # top 3 only
    assert out.summaries[str(D1)][0].canonical_name == "P0"
    assert len(out.summaries[str(D2)]) == 1


def test_summary_caps_ids_and_rejects_garbage(monkeypatch):
    _patch(monkeypatch)
    too_many = ",".join(str(uuid.uuid4()) for _ in range(101))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_entities_summary(ids=too_many, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 422
    out = asyncio.run(er.get_entities_summary(ids="not-a-uuid,,", db=FakeSession(), user=FakeUser()))
    assert out.summaries == {}
