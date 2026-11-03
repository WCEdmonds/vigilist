"""The "Mentions: <entity>" filter must narrow every path that acts on the
current result set: search, select-all-matching bulk actions (tags,
privilege, folders, imaging all resolve through resolve_query_docs), and the
CSV exports. One predicate (search.mentions_entity) is shared by all of
them and by GET /api/documents."""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import app.routers.export as rx
import app.routers.search as rs
import app.services.bulk_tagging as bt
import app.services.search as svc
from app.models import Document
from app.schemas import BulkTagQuery
from tests.fakes import FakeResult, FakeSession, FakeUser

ENT = uuid.uuid4()
EXISTS_SQL = "EXISTS (SELECT entity_mentions.id"


def _sql(conditions) -> str:
    return str(select(Document.id).where(*conditions).compile(dialect=postgresql.dialect()))


# --- the shared predicate ----------------------------------------------------

def test_mentions_entity_is_a_correlated_exists_with_one_bind():
    sql = _sql([svc.mentions_entity(ENT)])
    assert EXISTS_SQL in sql
    assert "entity_mentions.entity_id = %(entity_id_1)s" in sql
    assert "entity_mentions.document_id = documents.id" in sql


def test_build_search_conditions_entity_filter_and_unchanged_without():
    with_ent, _, has_criteria = svc.build_search_conditions("", production_id=1, entity_id=ENT)
    assert has_criteria is True  # the filter alone is enough to search
    assert EXISTS_SQL in _sql(with_ent)

    without, _, has_criteria = svc.build_search_conditions("", production_id=1)
    assert has_criteria is False
    assert "entity_mentions" not in _sql(without)


# --- search -------------------------------------------------------------------

def test_search_documents_applies_entity_filter_to_count_and_page():
    db = FakeSession(responders=[("count(", FakeResult(scalar=0))])
    asyncio.run(svc.search_documents(db, "contract", production_id=1, entity_id=ENT))
    assert len(db.executed) == 2
    assert all(EXISTS_SQL in s for s in db.executed)


def test_search_documents_without_entity_filter_is_unchanged():
    db = FakeSession(responders=[("count(", FakeResult(scalar=0))])
    asyncio.run(svc.search_documents(db, "contract", production_id=1))
    assert not any("entity_mentions" in s for s in db.executed)


def _patch_search_router(monkeypatch, seen):
    async def fake_accessible(db, user):
        return [1]

    async def fake_semantic(db, q, **kw):
        seen["semantic"] = True
        return [], 0

    async def fake_fulltext(db, q, **kw):
        seen.update(kw)
        return [], 0

    async def fake_log(*a, **kw):
        pass

    async def no_parents(db, ids):
        return {}

    monkeypatch.setattr(rs, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rs, "semantic_search", fake_semantic)
    monkeypatch.setattr(rs, "search_documents", fake_fulltext)
    monkeypatch.setattr(rs, "log_action", fake_log)
    monkeypatch.setattr(rs, "parent_refs_for_ids", no_parents)


def _search(**kw):
    params = dict(q="contract", production_id=1, page=1, per_page=50, sort="relevance",
                  mode="semantic", metadata=None, file_type=None, source_party=None,
                  source_type=None, email_address=None, email_scope="from", date_from=None,
                  date_to=None, tz=None, folder_id=None, entity_id=None)
    params.update(kw)
    return asyncio.run(rs.search(**params, db=FakeSession(), user=FakeUser()))


def test_search_endpoint_passes_entity_and_skips_semantic(monkeypatch):
    seen = {}
    _patch_search_router(monkeypatch, seen)
    _search(entity_id=ENT)
    # Semantic search can't apply filters, so the entity forces full-text.
    assert "semantic" not in seen
    assert seen["entity_id"] == ENT


def test_search_endpoint_without_entity_keeps_semantic(monkeypatch):
    seen = {}
    _patch_search_router(monkeypatch, seen)
    _search()
    assert seen.get("semantic") is True
    assert seen["entity_id"] is None


# --- select all matching (bulk tag / privilege / folder / imaging) -----------

def test_bulk_query_resolves_only_documents_mentioning_the_entity():
    db = FakeSession()
    asyncio.run(bt.resolve_query_docs(db, BulkTagQuery(production_id=1, entity_id=ENT), [1]))
    assert len(db.executed) == 1 and EXISTS_SQL in db.executed[0]


def test_bulk_query_without_entity_is_unchanged():
    db = FakeSession()
    asyncio.run(bt.resolve_query_docs(db, BulkTagQuery(q="contract", production_id=1), [1]))
    assert "entity_mentions" not in db.executed[0]


def test_bulk_query_schema_accepts_entity_id():
    q = BulkTagQuery.model_validate({"q": "", "entity_id": str(ENT)})
    assert q.entity_id == ENT


# --- exports --------------------------------------------------------------------

def _patch_export(monkeypatch, seen):
    async def fake_accessible(db, user):
        return [1]

    async def fake_search(db, q, **kwargs):
        seen.update(kwargs)
        return [], 0

    monkeypatch.setattr(rx, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(svc, "search_documents", fake_search)


def _csv_params(**kw):
    params = dict(q="", production_id=1, sort="relevance", metadata=None, file_type=None,
                  source_party=None, source_type=None, email_address=None, email_scope="from",
                  date_from=None, date_to=None, tz=None, folder_id=None)
    params.update(kw)
    return params


def test_search_csv_passes_entity_filter(monkeypatch):
    seen = {}
    _patch_export(monkeypatch, seen)
    asyncio.run(rx.export_search_csv(**_csv_params(entity_id=ENT), db=FakeSession(), user=FakeUser()))
    assert seen["entity_id"] == ENT


def test_search_csv_without_entity_is_unchanged(monkeypatch):
    seen = {}
    _patch_export(monkeypatch, seen)
    asyncio.run(rx.export_search_csv(**_csv_params(), db=FakeSession(), user=FakeUser()))
    assert seen["entity_id"] is None


def _documents_csv(monkeypatch, **kw):
    async def fake_accessible(db, user):
        return [1]
    monkeypatch.setattr(rx, "get_accessible_production_ids", fake_accessible)
    db = FakeSession()
    asyncio.run(rx.export_documents_csv(production_id=1, db=db, user=FakeUser(), **kw))
    return db.executed[0]


def test_documents_csv_applies_entity_filter(monkeypatch):
    assert EXISTS_SQL in _documents_csv(monkeypatch, entity_id=ENT)


def test_documents_csv_without_entity_is_unchanged(monkeypatch):
    assert "entity_mentions" not in _documents_csv(monkeypatch)


# --- every select-all-matching endpoint hands the entity to resolve_query_docs --

class _Stop(Exception):
    pass


def _capture_resolve(monkeypatch, seen):
    async def fake_resolve(db, query, accessible):
        seen["query"] = query
        raise _Stop
    monkeypatch.setattr(bt, "resolve_query_docs", fake_resolve)


def _run_capturing(coro):
    import pytest
    with pytest.raises(_Stop):
        asyncio.run(coro)


def test_bulk_tag_and_privilege_by_query_carry_the_entity(monkeypatch):
    import app.routers.tags as rt
    from app.models import Tag
    from app.schemas import BulkTagByQueryRequest

    seen = {}
    _capture_resolve(monkeypatch, seen)

    async def fake_accessible(db, user):
        return [1]
    monkeypatch.setattr(rt, "get_accessible_production_ids", fake_accessible)
    tag = Tag(id=5, category="privilege", name="Attorney-Client", is_privilege=True)
    for disposition in (None, "withhold"):
        body = BulkTagByQueryRequest(tag_id=5, disposition=disposition,
                                     query=BulkTagQuery(production_id=1, entity_id=ENT))
        db = FakeSession(get_objects={("Tag", 5): tag})
        _run_capturing(rt.bulk_tag_by_query(body=body, db=db, user=FakeUser()))
        assert seen["query"].entity_id == ENT


def test_folder_and_imaging_add_by_query_carry_the_entity(monkeypatch):
    import app.routers.folders as rf
    import app.routers.imaging as ri
    from types import SimpleNamespace
    from app.schemas import FolderAddDocuments

    seen = {}
    _capture_resolve(monkeypatch, seen)

    async def fake_folder(db, user, folder_id, write):
        return SimpleNamespace(id=folder_id, production_id=1)
    monkeypatch.setattr(rf, "_folder", fake_folder)
    body = FolderAddDocuments(query=BulkTagQuery(entity_id=ENT))
    _run_capturing(rf.add_folder_documents(folder_id=3, body=body, db=FakeSession(), user=FakeUser()))
    assert seen["query"].entity_id == ENT and seen["query"].production_id == 1

    seen.clear()

    async def fake_writer(db, user, production_id):
        return None
    monkeypatch.setattr(ri, "_require_writer", fake_writer)
    body = ri.PreparePagesRequest(query=BulkTagQuery(entity_id=ENT))
    _run_capturing(ri.prepare_pages_bulk(production_id=1, body=body, db=FakeSession(), user=FakeUser()))
    assert seen["query"].entity_id == ENT
