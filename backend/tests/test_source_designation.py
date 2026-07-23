"""Tests for document source designation (P0-SP5)."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.ingest as ri
from app.services.ingest import _stamp_source
from tests.fakes import FakeSession, FakeUser, _fill_timestamps


class FakeDocLike:
    def __init__(self, source_party=None, source_type=None):
        self.source_party = source_party
        self.source_type = source_type


class FakeJob:
    def __init__(self, field_mapping):
        self.field_mapping = field_mapping


def test_stamp_source_fills_from_job():
    doc = FakeDocLike()
    _stamp_source(doc, FakeJob({"source_party": "ABC Corp", "source_type": "received"}))
    assert doc.source_party == "ABC Corp"
    assert doc.source_type == "received"


def test_stamp_source_never_overwrites_mapped_value():
    doc = FakeDocLike(source_party="From DAT Column")
    _stamp_source(doc, FakeJob({"source_party": "Job Level", "source_type": "collection"}))
    assert doc.source_party == "From DAT Column"
    assert doc.source_type == "collection"


def test_stamp_source_handles_missing_mapping():
    doc = FakeDocLike()
    _stamp_source(doc, FakeJob(None))
    assert doc.source_party is None
    assert doc.source_type is None


# --- /api/ingest/process ----------------------------------------------------

class UuidFakeSession(FakeSession):
    """IngestJob ids are UUIDs; the base fake assigns ints."""

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        _fill_timestamps(obj)
        self.added.append(obj)


class FakeProduction:
    def __init__(self, owner="u1"):
        self.id = 1
        self.name = "Matter"
        self.owner_id = owner


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def _patch_ingest(monkeypatch):
    import app.services.tasks as task_service
    monkeypatch.setattr(task_service, "is_configured", lambda: False)


def test_process_rejects_bad_source_type(monkeypatch):
    _patch_ingest(monkeypatch)
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ri.start_processing(
            body={"production_id": 1, "source_format": "native",
                  "source_type": "maybe"},
            background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_process_folds_source_into_field_mapping(monkeypatch):
    _patch_ingest(monkeypatch)
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})
    out = asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native", "custodian": "Jane",
              "source_party": "ABC Corp", "source_type": "received",
              "total_files": 3},
        background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    job = db.added[0]
    assert job.field_mapping["custodian"] == "Jane"
    assert job.field_mapping["source_party"] == "ABC Corp"
    assert job.field_mapping["source_type"] == "received"
    assert out.total_files == 3


# --- search filters ---------------------------------------------------------

from app.services.search import search_documents
from tests.fakes import FakeResult


def test_search_applies_source_filters():
    db = FakeSession()
    asyncio.run(search_documents(
        db, "", production_id=1, accessible_production_ids=[1],
        source_party="ABC Corp", source_type="received"))
    joined = "\n".join(db.executed)
    assert "documents.source_party" in joined
    assert "documents.source_type" in joined


def test_search_source_filter_alone_is_enough():
    # the no-criteria early return must not swallow source-only browsing
    db = FakeSession()
    results, total = asyncio.run(search_documents(
        db, "", accessible_production_ids=[1], source_type="collection"))
    assert results == []
    assert len(db.executed) >= 1  # it actually queried


def test_source_parties_endpoint(monkeypatch):
    import app.routers.documents as rd

    async def fake_accessible(db, user):
        return [1]

    monkeypatch.setattr(rd, "get_accessible_production_ids", fake_accessible)
    db = FakeSession(responders=[
        ("source_party", FakeResult(rows=[("ABC Corp",), ("Our Collection",)])),
    ])
    out = asyncio.run(rd.list_source_parties(production_id=1, db=db, user=FakeUser()))
    assert out == {"source_parties": ["ABC Corp", "Our Collection"]}


def test_source_parties_403(monkeypatch):
    import app.routers.documents as rd

    async def fake_accessible(db, user):
        return [2]

    monkeypatch.setattr(rd, "get_accessible_production_ids", fake_accessible)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rd.list_source_parties(production_id=1, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_collection_filter_includes_undesignated():
    # Outgoing mode must keep NULL-source (legacy) documents visible.
    db = FakeSession()
    asyncio.run(search_documents(
        db, "", accessible_production_ids=[1], source_type="collection"))
    joined = "\n".join(db.executed)
    assert "IS DISTINCT FROM" in joined


def test_received_filter_is_exact():
    db = FakeSession()
    asyncio.run(search_documents(
        db, "", accessible_production_ids=[1], source_type="received"))
    joined = "\n".join(db.executed)
    assert "IS DISTINCT FROM" not in joined
    assert "documents.source_type" in joined
