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
        self.render_status = kw.get("render_status", "not_started")
        self.render_error = None
        self.rendered_at = None
        self.package_status = kw.get("package_status", "not_started")
        self.package_error = None
        self.package_path = kw.get("package_path", None)
        self.packaged_at = None


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
        self.output_path = kw.get("output_path", None)


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


# --- POST /production-sets/{id}/documents ----------------------------------
# FakeSession dispatches on the FIRST matching substring, so register
# responders in this order: "document_tags", "family_id IN",
# "document_duplicates", "production_set_items", "documents.production_id"
# (the last is a substring of several queries' WHERE clauses).

from app.schemas import ProductionSetAddDocuments, ProductionSetRemoveDocuments


def test_add_explicit_docs(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("documents.production_id", FakeResult(rows=[(d1, 1, None), (d2, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1, body=ProductionSetAddDocuments(document_ids=[d1, d2]),
        db=db, user=FakeUser()))
    assert out == {"added": 2, "skipped_existing": 0,
                   "skipped_duplicates": 0, "families_added": 0}
    assert len(db.added) == 2


def test_add_by_tag(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("document_tags", FakeResult(rows=[(d1,), (d2,)])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None), (d2, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1, body=ProductionSetAddDocuments(tag_id=5), db=db, user=FakeUser()))
    assert out["added"] == 2


def test_add_doc_from_other_matter_422(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("documents.production_id", FakeResult(rows=[(d1, 2, None)])),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(document_ids=[d1]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_add_unknown_doc_422(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(document_ids=[uuid4()]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_add_nothing_specified_422(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_add_include_families_pulls_family_members(monkeypatch):
    _patch(monkeypatch)
    d1, d3 = uuid4(), uuid4()  # d1 explicit (family F1); d3 = its attachment
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("family_id IN", FakeResult(rows=[(d1,), (d3,)])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, "F1")])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1,
        body=ProductionSetAddDocuments(document_ids=[d1], include_families=True),
        db=db, user=FakeUser()))
    assert out["added"] == 2
    assert out["families_added"] == 1


def test_add_exclude_duplicates_keeps_primary(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()  # same hash group; d2 has the lower control -> primary
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("document_tags", FakeResult(rows=[(d1,), (d2,)])),
            ("document_duplicates", FakeResult(rows=[(10, d1, "C-2"), (10, d2, "C-1")])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None), (d2, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1,
        body=ProductionSetAddDocuments(tag_id=5, exclude_duplicates=True),
        db=db, user=FakeUser()))
    assert out["added"] == 1
    assert out["skipped_duplicates"] == 1


def test_add_exclude_duplicates_never_drops_explicit_ids(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()  # d1 explicitly listed but NOT the primary
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("document_duplicates", FakeResult(rows=[(10, d1, "C-2"), (10, d2, "C-1")])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1,
        body=ProductionSetAddDocuments(document_ids=[d1], exclude_duplicates=True),
        db=db, user=FakeUser()))
    assert out["added"] == 1
    assert out["skipped_duplicates"] == 0


def test_add_skips_existing_members(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("production_set_items", FakeResult(rows=[(d1,)])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None), (d2, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1, body=ProductionSetAddDocuments(document_ids=[d1, d2]),
        db=db, user=FakeUser()))
    assert out["added"] == 1
    assert out["skipped_existing"] == 1


def test_add_to_locked_set_409(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="locked")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(document_ids=[uuid4()]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_add_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(document_ids=[uuid4()]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 403


# --- DELETE /production-sets/{id}/documents --------------------------------

def test_remove_documents_draft(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    out = asyncio.run(rps.remove_documents(
        set_id=1, body=ProductionSetRemoveDocuments(document_ids=[d1]),
        db=db, user=FakeUser()))
    assert out == {"removed": 1}


def test_remove_documents_locked_409(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="locked")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.remove_documents(
            set_id=1, body=ProductionSetRemoveDocuments(document_ids=[uuid4()]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 409


# --- POST /production-sets/{id}/lock ---------------------------------------
# Responder order for lock tests: "is_privilege", "redactions",
# "production_set_items", "documents.page_count".


def test_lock_assigns_and_snapshots(monkeypatch):
    _patch(monkeypatch)
    d1, d2, d3 = uuid4(), uuid4(), uuid4()
    items = [FakeItem(d1, item_id=1), FakeItem(d2, item_id=2), FakeItem(d3, item_id=3)]
    ps = FakePS()
    doc_rows = [
        # (id, control, family_id, custodian, date_sent, date_received,
        #  page_count, privilege_disposition)
        (d1, "C-1", None, "Alice", TS, None, 5, None),  # privilege tag -> withhold, 1 page
        (d2, "C-2", None, "Bob", TS, None, 3, None),    # redactions -> redact_in_part
        (d3, "C-3", None, "Cara", TS, None, 2, None),   # nothing -> produce
    ]
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("is_privilege", FakeResult(rows=[(d1,)])),
            ("redactions", FakeResult(rows=[(d2, 4)])),
            ("production_set_items", FakeResult(items=items)),
            ("documents.page_count", FakeResult(rows=doc_rows)),
        ],
    )
    out = asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert out.doc_count == 3
    assert out.page_count == 1 + 3 + 2
    assert out.bates_begin == "SMITH000001"
    assert out.bates_end == "SMITH000006"
    by_doc = {i.document_id: i for i in items}
    assert by_doc[d1].disposition == "withhold"
    assert by_doc[d1].pages == 1
    assert (by_doc[d1].bates_begin, by_doc[d1].bates_end) == ("SMITH000001", "SMITH000001")
    assert by_doc[d2].disposition == "redact_in_part"
    assert (by_doc[d2].bates_begin, by_doc[d2].bates_end) == ("SMITH000002", "SMITH000004")
    assert by_doc[d3].disposition == "produce"
    assert (by_doc[d3].bates_begin, by_doc[d3].bates_end) == ("SMITH000005", "SMITH000006")
    assert [by_doc[d].sort_order for d in (d1, d2, d3)] == [1, 2, 3]
    assert ps.status == "locked"
    assert ps.locked_by == "u1"
    assert ps.locked_at is not None


def test_lock_produce_override_keeps_full_pages(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    items = [FakeItem(d1, item_id=1)]
    ps = FakePS()
    doc_rows = [(d1, "C-1", None, None, None, None, 4, "produce")]
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("is_privilege", FakeResult(rows=[(d1,)])),  # tagged, but override wins
            ("redactions", FakeResult(rows=[])),
            ("production_set_items", FakeResult(items=items)),
            ("documents.page_count", FakeResult(rows=doc_rows)),
        ],
    )
    out = asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert items[0].disposition == "produce"
    assert items[0].pages == 4
    assert out.page_count == 4


def test_lock_empty_set_422(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_lock_already_locked_409(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="locked")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_lock_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 403


# --- render endpoints (P2-2) ------------------------------------------------

class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def test_render_trigger_requires_locked(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="draft")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.render_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_render_trigger_409_while_running(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked", render_status="rendering")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.render_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_render_trigger_batches_and_fallback(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(rps.tasks, "is_configured", lambda: False)
    ps = FakePS(status="locked")
    doc_ids = [(uuid4(),) for _ in range(30)]
    bg = FakeBackgroundTasks()
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("production_set_items", FakeResult(rows=doc_ids))],
    )
    out = asyncio.run(rps.render_production_set(
        set_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert out == {"documents": 30, "batches": 2}  # 25 + 5
    assert ps.render_status == "rendering"
    assert len(bg.tasks) == 1  # one inline runner covering all batches


def test_render_trigger_empty_set_422(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.render_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_render_worker_calls_pipeline(monkeypatch):
    calls = {}

    async def fake_render_batch(db, set_id, document_ids):
        calls["batch"] = (set_id, len(document_ids))
        return len(document_ids)

    async def fake_finalize(db, set_id):
        calls["finalized"] = set_id
        return True

    monkeypatch.setattr(rps, "render_batch", fake_render_batch)
    monkeypatch.setattr(rps, "finalize_if_complete", fake_finalize)
    d1, d2 = uuid4(), uuid4()
    out = asyncio.run(rps.render_batch_handler(
        body={"set_id": 1, "document_ids": [str(d1), str(d2)]},
        db=FakeSession(), _verified=None))
    assert out == {"rendered": 2}
    assert calls == {"batch": (1, 2), "finalized": 1}


def test_produced_pdf_404_before_render(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS(status="locked")},
        responders=[("production_set_items", FakeResult(items=[FakeItem(d1)]))],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.get_produced_pdf(set_id=1, document_id=d1, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_produced_pdf_redirects_to_signed_url(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(rps, "get_signed_url",
                        lambda path, **kw: f"https://signed.example/{path}")
    d1 = uuid4()
    item = FakeItem(d1, bates_begin="SMITH000004")
    item.output_path = "productions/1/production_sets/1/SMITH000004.pdf"
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS(status="locked")},
        responders=[("production_set_items", FakeResult(items=[item]))],
    )
    out = asyncio.run(rps.get_produced_pdf(set_id=1, document_id=d1, db=db, user=FakeUser()))
    assert out.status_code == 307
    assert out.headers["location"].endswith("SMITH000004.pdf")


def test_detail_includes_render_progress(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    ps = FakePS(status="locked", render_status="rendering")
    i1 = FakeItem(d1, item_id=1, sort_order=1, bates_begin="SMITH000001",
                  bates_end="SMITH000001", pages=1)
    i1.output_path = "productions/1/production_sets/1/SMITH000001.pdf"
    i2 = FakeItem(d2, item_id=2, sort_order=2, bates_begin="SMITH000002",
                  bates_end="SMITH000002", pages=1)
    i2.output_path = None
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("FROM production_set_items", FakeResult(items=[i1, i2]))],
    )
    out = asyncio.run(rps.get_production_set(set_id=1, db=db, user=FakeUser()))
    assert out.render_status == "rendering"
    assert out.rendered_count == 1


# --- manifest + packaging endpoints (P2-3) ---------------------------------

def test_manifest_requires_locked(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="draft")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.get_manifest(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_manifest_returns_continuity(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    item = FakeItem(d1, item_id=1, sort_order=1, bates_begin="SMITH000001",
                    bates_end="SMITH000002", pages=2, disposition="produce")
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS(status="locked")},
        responders=[("FROM production_set_items", FakeResult(items=[item]))],
    )
    out = asyncio.run(rps.get_manifest(set_id=1, db=db, user=FakeUser()))
    assert out["continuity"]["ok"] is True
    assert out["counts"]["documents"] == 1


def test_package_requires_rendered(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked", render_status="rendering")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.package_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_package_409_while_packaging(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked", render_status="rendered", package_status="packaging")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.package_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_package_trigger_fallback(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(rps.tasks, "is_configured", lambda: False)
    ps = FakePS(status="locked", render_status="rendered")
    bg = FakeBackgroundTasks()
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("count", FakeResult(scalar=4))],
    )
    out = asyncio.run(rps.package_production_set(
        set_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert out == {"documents": 4}
    assert ps.package_status == "packaging"
    assert len(bg.tasks) == 1


def test_package_worker_delegates(monkeypatch):
    called = {}

    async def fake_package_set(db, set_id):
        called["set"] = set_id

    monkeypatch.setattr(rps, "package_set", fake_package_set)
    out = asyncio.run(rps.package_worker_handler(
        body={"set_id": 5}, db=FakeSession(), _verified=None))
    assert out == {"ok": True}
    assert called == {"set": 5}


def test_package_download_404_until_packaged(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="locked")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.download_package(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_package_download_redirects(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(rps, "get_signed_url",
                        lambda path, **kw: f"https://signed.example/{path}")
    ps = FakePS(status="locked", package_status="packaged",
                package_path="productions/1/production_sets/1/package/SMITH_production.zip")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    out = asyncio.run(rps.download_package(set_id=1, db=db, user=FakeUser()))
    assert out.status_code == 307
    assert out.headers["location"].endswith("SMITH_production.zip")
