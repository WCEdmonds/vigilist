"""GET /api/documents?entity_id=: every document that mentions one entity."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.routers.documents as rd
from app.models import Entity
from tests.fakes import FakeResult, FakeSession, FakeUser

ENT_ID = uuid.uuid4()


def _entity(production_id=1):
    return Entity(id=ENT_ID, production_id=production_id, entity_type="org",
                  canonical_name="ACME Corp", aliases=[], attributes={}, mention_count=3)


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(rd, "get_accessible_production_ids", fake_accessible)

    async def no_parents(db, ids):
        return {}
    monkeypatch.setattr(rd, "parent_refs_for_ids", no_parents)


def _call(db, **kw):
    params = dict(production_id=None, tag_id=None, has_annotations=None, file_type=None,
                  source_party=None, source_type=None, sort="bates", cluster_id=None,
                  ai_decision=None, folder_id=None, entity_id=None, page=1, per_page=50)
    params.update(kw)
    return asyncio.run(rd.list_documents(**params, db=db, user=FakeUser()))


def test_entity_filter_unknown_entity_is_404(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _call(FakeSession(), entity_id=ENT_ID)
    assert exc.value.status_code == 404


def test_entity_filter_out_of_scope_entity_is_404(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    db = FakeSession(get_objects={("Entity", ENT_ID): _entity(production_id=1)})
    with pytest.raises(HTTPException) as exc:
        _call(db, entity_id=ENT_ID)
    assert exc.value.status_code == 404


def test_entity_filter_applies_exists_to_page_and_count(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("Entity", ENT_ID): _entity()},
                     responders=[("count(documents.id)", FakeResult(scalar=3))])
    out = _call(db, production_id=1, entity_id=ENT_ID, tag_id=5, sort="date_desc", page=2)
    assert out.total == 3
    count_sql = next(s for s in db.executed if "count(documents.id)" in s)
    page_sql = next(s for s in db.executed if s.startswith("SELECT documents.") and "LIMIT" in s)
    for sql in (count_sql, page_sql):
        assert "EXISTS (SELECT" in sql
        assert "entity_mentions.entity_id" in sql
        assert "entity_mentions.document_id = documents.id" in sql
        # Composes with the other filters.
        assert "document_tags" in sql
    # One bind for the entity, never a Python list of document ids.
    assert "IN (__[POSTCOMPILE" not in page_sql.split("LIMIT")[0].split("entity_mentions")[1]


def test_no_entity_filter_leaves_query_untouched(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[("count(documents.id)", FakeResult(scalar=0))])
    _call(db, production_id=1)
    assert not any("entity_mentions" in s for s in db.executed)
