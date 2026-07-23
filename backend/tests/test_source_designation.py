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
