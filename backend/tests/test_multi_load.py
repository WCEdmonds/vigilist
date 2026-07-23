"""Tests for multi-load matters: load-prefix threading + control offsets."""

import asyncio

import pytest
from fastapi import HTTPException

import app.routers.ingest as ri
from app.services.ingest import compute_control_offset
from tests.fakes import FakeResult, FakeSession, FakeUser
from tests.test_source_designation import (
    FakeBackgroundTasks,
    FakeProduction,
    UuidFakeSession,
    _patch_ingest,
)


# --- compute_control_offset -------------------------------------------------

def test_offset_empty():
    assert compute_control_offset([], "ACME") == 0


def test_offset_max_tail():
    vals = ["ACME 000001", "ACME 000042", "ACME 000007"]
    assert compute_control_offset(vals, "ACME") == 42


def test_offset_ignores_other_prefixes_and_shapes():
    vals = ["OTHER 000099", "ACME000100", "ACME 00x100", None, "ACME 000003 .0001"]
    assert compute_control_offset(vals, "ACME") == 0


def test_offset_prefix_regex_escaped():
    assert compute_control_offset(["A.B 000009"], "A.B") == 9
    assert compute_control_offset(["AXB 000009"], "A.B") == 0


# --- listing/bootstrap namespaces ------------------------------------------

def test_list_pdf_sources_prefix(monkeypatch):
    import app.services.ingest_pdf as pdf_mod
    seen = {}

    def fake_list(prefix):
        seen["prefix"] = prefix
        return []

    monkeypatch.setattr(pdf_mod, "list_files", fake_list)
    pdf_mod.list_pdf_sources(7)
    assert seen["prefix"] == "productions/7/raw/"
    pdf_mod.list_pdf_sources(7, "loads/ab12cd34/")
    assert seen["prefix"] == "productions/7/raw/loads/ab12cd34/"


def test_list_native_sources_prefix(monkeypatch):
    import app.services.ingest_native as native_mod
    seen = {}

    def fake_list(prefix):
        seen["prefix"] = prefix
        return []

    monkeypatch.setattr(native_mod, "list_files", fake_list)
    native_mod.list_native_sources(7, "loads/x1/")
    assert seen["prefix"] == "productions/7/raw/loads/x1/"


def test_bootstrap_looks_in_load_namespace(monkeypatch):
    import app.services.ingest as ingest_mod
    import app.services.storage as storage_mod
    seen = {}

    def fake_list(prefix):
        seen["prefix"] = prefix
        return []

    monkeypatch.setattr(storage_mod, "list_files", fake_list)
    with pytest.raises(FileNotFoundError):
        ingest_mod.bootstrap_ingest_source(7, "loads/x1/")
    assert seen["prefix"] == "productions/7/raw/loads/x1/DATA/"


# --- /ingest/process --------------------------------------------------------

def test_process_folds_load_prefix_and_offset(monkeypatch):
    _patch_ingest(monkeypatch)
    db = UuidFakeSession(
        get_objects={("Production", 1): FakeProduction()},
        responders=[("bates_begin", FakeResult(rows=[("MATTER 000040",), ("MATTER 000012",)]))],
    )
    asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native", "total_files": 2,
              "load_id": "ab12cd34"},
        background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    job = db.added[0]
    assert job.field_mapping["load_prefix"] == "loads/ab12cd34/"
    assert job.field_mapping["control_offset"] == 40


def test_process_rejects_bad_load_id(monkeypatch):
    _patch_ingest(monkeypatch)
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ri.start_processing(
            body={"production_id": 1, "source_format": "native",
                  "load_id": "../evil"},
            background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_process_without_load_id_keeps_legacy_behavior(monkeypatch):
    _patch_ingest(monkeypatch)
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})
    asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native", "total_files": 1},
        background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    job = db.added[0]
    assert "load_prefix" not in job.field_mapping
    assert "control_offset" not in job.field_mapping


# --- /ingest/analyze --------------------------------------------------------

def test_analyze_passes_load_prefix(monkeypatch):
    captured = {}

    async def fake_threadpool(fn, *args):
        captured["args"] = args
        return {"columns": []}

    monkeypatch.setattr(ri, "run_in_threadpool", fake_threadpool)
    db = FakeSession(get_objects={("Production", 1): FakeProduction()})
    out = asyncio.run(ri.analyze_ingest(
        body={"production_id": 1, "load_id": "x1"}, db=db, user=FakeUser()))
    assert captured["args"] == (1, "loads/x1/")
    assert out == {"columns": []}
