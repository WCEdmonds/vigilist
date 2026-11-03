"""Prepare-pages endpoints and the imaging worker."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.imaging as ri
from app.schemas import BulkTagQuery, FolderAddDocuments
from tests.fakes import FakeResult, FakeSession, FakeUser


def _doc(**kw):
    base = dict(id=uuid4(), production_id=4, bates_begin="ACME 1", family_id="ACME 1",
                file_type="email", native_path="productions/4/raw/box.pst", image_paths=[],
                imaging_status=None, imaging_error=None, imaging_requested_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _patch(monkeypatch, *, role="reviewer", matched=None):
    state = {"dispatched": [], "audit": [], "query": None}

    async def fake_accessible(db, user):
        return [4]

    async def fake_role(db, user, pid):
        return role

    async def fake_log(db, user, action, resource_type, resource_id=None, **kw):
        state["audit"].append((action, kw.get("details")))

    async def fake_resolve_query(db, query, acc):
        state["query"] = query
        return dict(matched or {})

    async def fake_resolve_explicit(db, ids):
        return dict(matched or {})

    async def fake_expand(db, docs):
        return dict(docs)

    monkeypatch.setattr(ri, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(ri, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(ri, "log_action", fake_log)
    async def fake_dispatch(ids, user_id):
        state["dispatched"].append(list(ids))

    monkeypatch.setattr(ri, "_dispatch", fake_dispatch)
    monkeypatch.setattr(ri.bt, "resolve_query_docs", fake_resolve_query)
    monkeypatch.setattr(ri.bt, "resolve_explicit_docs", fake_resolve_explicit)
    monkeypatch.setattr(ri.bt, "expand_attachments", fake_expand)
    return state


def _single_db(doc, redactions=0):
    return FakeSession(get_objects={("Document", doc.id): doc},
                       responders=[("count(redactions.id)", FakeResult(scalar=redactions))])


def test_prepare_single_queues_and_enqueues_once(monkeypatch):
    state = _patch(monkeypatch)
    doc = _doc()
    db = _single_db(doc)
    assert asyncio.run(ri.prepare_pages(doc.id, db=db, user=FakeUser())) == {"imaging_status": "queued"}
    assert doc.imaging_requested_at is not None
    assert asyncio.run(ri.prepare_pages(doc.id, db=db, user=FakeUser())) == {"imaging_status": "queued"}
    assert state["dispatched"] == [[str(doc.id)]]


def test_stale_running_job_is_requeued(monkeypatch):
    state = _patch(monkeypatch)
    doc = _doc(imaging_status="running",
               imaging_requested_at=datetime.now(timezone.utc) - timedelta(minutes=40))
    asyncio.run(ri.prepare_pages(doc.id, db=_single_db(doc), user=FakeUser()))
    assert doc.imaging_status == "queued" and state["dispatched"] == [[str(doc.id)]]


@pytest.mark.parametrize("doc_kw,redactions,status", [
    (dict(file_type="xlsx", native_path="productions/4/attachments/x/b.xlsx", family_id="ACME 0"), 0, 422),
    (dict(image_paths=["productions/4/converted/a.jpg"]), 0, 422),
    ({}, 3, 409),
])
def test_prepare_single_rejects_office_imaged_and_redacted(monkeypatch, doc_kw, redactions, status):
    state = _patch(monkeypatch)
    doc = _doc(**doc_kw)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ri.prepare_pages(doc.id, db=_single_db(doc, redactions), user=FakeUser()))
    assert exc.value.status_code == status and state["dispatched"] == []


def test_readonly_cannot_prepare(monkeypatch):
    _patch(monkeypatch, role="readonly")
    doc = _doc()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ri.prepare_pages(doc.id, db=_single_db(doc), user=FakeUser()))
    assert exc.value.status_code == 403


def test_prepare_bulk_counts_and_skips(monkeypatch):
    todo = _doc()
    imaged = _doc(image_paths=["productions/4/converted/a.jpg"])
    queued = _doc(imaging_status="queued", imaging_requested_at=datetime.now(timezone.utc))
    office = _doc(file_type="xlsx", native_path="productions/4/attachments/x/b.xlsx", family_id="ACME 0")
    redacted = _doc()
    docs = [todo, imaged, queued, office, redacted]
    state = _patch(monkeypatch, matched={d.id: 4 for d in docs})
    db = FakeSession(responders=[
        ("count(redactions.id)", FakeResult(rows=[(redacted.id, 2)])),
        ("FROM documents", FakeResult(items=docs)),
    ])
    out = asyncio.run(ri.prepare_pages_bulk(4, FolderAddDocuments(doc_ids=[d.id for d in docs]),
                                            db=db, user=FakeUser()))
    assert out == {"queued": 1, "already_imaged": 1, "in_progress": 1, "unsupported": 1,
                   "has_redactions": 1, "failed_to_start": 0, "entity_id": None}
    assert state["dispatched"] == [[str(todo.id)]]
    assert todo.imaging_status == "queued"


def test_prepare_bulk_query_pinned_to_production(monkeypatch):
    state = _patch(monkeypatch, matched={})
    db = FakeSession(responders=[("FROM documents", FakeResult(items=[]))])
    asyncio.run(ri.prepare_pages_bulk(4, FolderAddDocuments(query=BulkTagQuery(production_id=99, q="x")),
                                      db=db, user=FakeUser()))
    assert state["query"].production_id == 4


def test_worker_images_each_and_returns_200_on_refusal(monkeypatch):
    calls = []

    async def fake_image(db, doc_id):
        calls.append(doc_id)
        if len(calls) == 1:
            raise ri.ImagingRefused("has redactions")

    monkeypatch.setattr(ri, "image_document", fake_image)
    monkeypatch.setattr(ri, "log_action", lambda *a, **k: asyncio.sleep(0))
    a, b = uuid4(), uuid4()
    db = FakeSession()
    out = asyncio.run(ri.imaging_worker({"document_ids": [str(a), str(b)], "requested_by": "u1"},
                                        db=db, _verified=None))
    assert calls == [a, b] and out == {"processed": 2}


def test_enqueue_imaging_one_document_per_task(monkeypatch):
    import app.services.tasks as t
    sent = []
    monkeypatch.setattr(t, "_enqueue", lambda path, body, **kw: sent.append((path, body)))
    t.enqueue_imaging([str(i) for i in range(3)], "u1")
    assert [b["document_ids"] for _, b in sent] == [["0"], ["1"], ["2"]]
    assert all(p == "/api/documents/imaging-worker" and b["requested_by"] == "u1" for p, b in sent)


def test_bulk_dispatch_failure_marks_documents_failed(monkeypatch):
    """I-6: a failed enqueue must not leave documents 'queued' with no task."""
    todo = _doc()
    _patch(monkeypatch, matched={todo.id: 4})

    async def boom(ids, user_id):
        raise RuntimeError("Cloud Tasks unavailable")

    monkeypatch.setattr(ri, "_dispatch", boom)
    db = FakeSession(responders=[("FROM documents", FakeResult(items=[todo]))])
    out = asyncio.run(ri.prepare_pages_bulk(4, FolderAddDocuments(doc_ids=[todo.id]), db=db, user=FakeUser()))
    assert todo.imaging_status == "failed" and "could not start" in todo.imaging_error
    assert out["queued"] == 0 and out["failed_to_start"] == 1
