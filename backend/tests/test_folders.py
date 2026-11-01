"""Folders: pull a subset of a matter's documents (e.g. one time period) into a
named folder reviewers work from."""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

import app.routers.folders as rf
from app.schemas import BulkTagQuery, FolderAddDocuments, FolderCreate, FolderRemoveDocuments
from app.services.search import build_search_conditions
from tests.fakes import FakeResult, FakeSession, FakeUser

A, B, ATT = uuid4(), uuid4(), uuid4()
NOW = datetime(2026, 11, 1, 12, 0)


def _folder(production_id=4):
    return SimpleNamespace(id=9, production_id=production_id, name="March 2026", created_at=NOW)


def _patch(monkeypatch, *, role="reviewer", accessible=(4,), matched=None, family=None):
    state = {"audit": [], "query": None}

    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, pid):
        return role

    async def fake_log(db, user, action, resource_type, resource_id=None, **kw):
        state["audit"].append((action, kw.get("details")))

    async def fake_resolve_query(db, query, acc):
        state["query"] = (query, acc)
        return dict(matched or {})

    async def fake_resolve_explicit(db, ids):
        return dict(matched or {})

    async def fake_expand(db, docs):
        return {**docs, **(family or {})}

    monkeypatch.setattr(rf, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rf, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rf, "log_action", fake_log)
    monkeypatch.setattr(rf.bt, "resolve_query_docs", fake_resolve_query)
    monkeypatch.setattr(rf.bt, "resolve_explicit_docs", fake_resolve_explicit)
    monkeypatch.setattr(rf.bt, "expand_attachments", fake_expand)
    return state


def test_add_by_date_range_search_includes_attachments_and_skips_existing(monkeypatch):
    state = _patch(monkeypatch, matched={A: 4, B: 4}, family={ATT: 4})
    db = FakeSession(
        get_objects={("Folder", 9): _folder()},
        responders=[
            ("SELECT folder_documents.document_id", FakeResult(rows=[(B,)])),  # B already in
            ("count(*)", FakeResult(scalar=3)),
        ],
    )
    body = FolderAddDocuments(query=BulkTagQuery(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31),
                                                 production_id=99))
    out = asyncio.run(rf.add_folder_documents(9, body, db=db, user=FakeUser()))
    assert out == {"matched": 2, "attachments_added": 1, "added": 2, "already_in_folder": 1,
                   "document_count": 3}
    query, acc = state["query"]
    assert query.production_id == 4 and acc == [4]   # pinned to the folder's matter
    assert query.date_from == date(2026, 3, 1)
    assert state["audit"][0][0] == "folder_documents_added"
    assert db.commits == 1


def test_add_rejects_documents_from_another_matter(monkeypatch):
    _patch(monkeypatch, matched={A: 5})
    db = FakeSession(get_objects={("Folder", 9): _folder()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rf.add_folder_documents(9, FolderAddDocuments(doc_ids=[A]), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_readonly_cannot_change_folders(monkeypatch):
    _patch(monkeypatch, role="readonly")
    db = FakeSession(get_objects={("Folder", 9): _folder()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rf.remove_folder_documents(9, FolderRemoveDocuments(doc_ids=[A]), db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_folder_in_inaccessible_matter_is_not_found(monkeypatch):
    _patch(monkeypatch, accessible=(1,))
    db = FakeSession(get_objects={("Folder", 9): _folder()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rf.add_folder_documents(9, FolderAddDocuments(doc_ids=[A]), db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_duplicate_folder_name_conflicts(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[("lower(folders.name)", FakeResult(items=[(1,)]))])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rf.create_folder(4, FolderCreate(name="march 2026"), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_remove_deletes_membership_only(monkeypatch):
    state = _patch(monkeypatch)
    db = FakeSession(
        get_objects={("Folder", 9): _folder()},
        responders=[("DELETE FROM folder_documents", FakeResult(rowcount=2)),
                    ("count(*)", FakeResult(scalar=1))],
    )
    out = asyncio.run(rf.remove_folder_documents(9, FolderRemoveDocuments(doc_ids=[A, B]),
                                                 db=db, user=FakeUser()))
    assert out == {"removed": 2, "document_count": 1}
    assert not any("DELETE FROM documents" in sql for sql in db.executed)
    assert state["audit"][0] == ("folder_documents_removed", {"removed": 2, "requested": 2})


def test_search_folder_filter_counts_as_criteria_and_compiles():
    conditions, _, has_criteria = build_search_conditions("", folder_id=9)
    assert has_criteria
    sql = str(conditions[-1].compile(dialect=postgresql.dialect()))
    assert "folder_documents.folder_id" in sql
