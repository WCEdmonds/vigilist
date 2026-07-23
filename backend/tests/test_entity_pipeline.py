"""Entity extraction stage: batching, idempotency marker, per-doc failure
isolation, stop-on-fully-failed-batch (mirrors the summaries stage contract).

Also covers the /extract-entities trigger endpoint (role check, Cloud Tasks
enqueue vs BackgroundTasks fallback, audit logging) using the fake-session
pattern from tests/test_redaction_endpoints.py."""

import asyncio
import uuid
from datetime import datetime

import pytest
from fastapi import BackgroundTasks, HTTPException

import app.routers.ingest as ingest_router
import app.services.pipeline as pipeline
import app.services.tasks as task_service


class FakeDoc:
    def __init__(self, text="Jorge Rivera wrote this."):
        self.id = uuid.uuid4()
        self.production_id = 1
        self.text_content = text
        self.email_from = None
        self.email_to = None
        self.email_cc = None
        self.email_bcc = None
        self.entities_extracted_at = None


def test_entities_stage_registered_between_summaries_and_brief():
    assert pipeline.STAGES == ("clustering", "summaries", "entities", "brief")
    assert "entities" in pipeline._STAGE_RUNNERS


class FakeBatchSession:
    """Async-context session that serves docs by id (the runner re-fetches
    each doc inside a fresh session before extracting)."""

    def __init__(self, docs):
        self._docs = {d.id: d for d in docs}

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, model, key): return self._docs.get(key)
    async def commit(self): pass


def test_run_entities_marks_docs_and_stops_when_none_left(monkeypatch):
    docs = [FakeDoc(), FakeDoc()]
    batches = [docs, []]  # first select returns docs, second returns none

    async def fake_pending(production_id, limit):
        return batches.pop(0)

    async def fake_extract_one(db, doc):
        doc.entities_extracted_at = datetime(2026, 7, 22)
        return True

    monkeypatch.setattr(pipeline, "_pending_extraction_docs", fake_pending)
    monkeypatch.setattr(pipeline, "_extract_one_document", fake_extract_one)
    monkeypatch.setattr(pipeline, "async_session", lambda: FakeBatchSession(docs))
    asyncio.run(pipeline._run_entities(1))
    assert all(d.entities_extracted_at for d in docs)


def test_run_entities_no_false_failure_when_batch_fully_skipped(monkeypatch):
    """All docs in a batch vanish at re-fetch (deleted, or already marked by
    a concurrent worker) -> attempted stays 0 -> must not raise, just move on."""
    batches = [[FakeDoc(), FakeDoc()], []]  # first select returns docs, second none

    async def fake_pending(production_id, limit):
        return batches.pop(0)

    async def fake_extract_one(db, doc):
        raise AssertionError("must not be called — every doc is skipped at re-fetch")

    monkeypatch.setattr(pipeline, "_pending_extraction_docs", fake_pending)
    monkeypatch.setattr(pipeline, "_extract_one_document", fake_extract_one)
    # FakeBatchSession serving no docs -> db.get returns None for every id.
    monkeypatch.setattr(pipeline, "async_session", lambda: FakeBatchSession([]))
    asyncio.run(pipeline._run_entities(1))  # must complete without raising


def test_run_entities_raises_after_fully_failed_batch(monkeypatch):
    import pytest
    calls = {"n": 0}
    doc = FakeDoc()

    async def fake_pending(production_id, limit):
        calls["n"] += 1
        return [doc]  # same doc forever — must not spin

    async def fake_extract_one(db, d):
        return False  # extraction failed; doc left unmarked

    monkeypatch.setattr(pipeline, "_pending_extraction_docs", fake_pending)
    monkeypatch.setattr(pipeline, "_extract_one_document", fake_extract_one)
    monkeypatch.setattr(pipeline, "async_session", lambda: FakeBatchSession([doc]))
    with pytest.raises(RuntimeError):
        asyncio.run(pipeline._run_entities(1))
    assert calls["n"] == 1  # gave up after one all-failed batch (stage marked failed by the pipeline wrapper)


# --- /productions/{production_id}/extract-entities trigger endpoint -------

class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"
        self.display_name = uid


class FakeSession:
    async def commit(self):
        pass


def _patch_role(monkeypatch, role):
    async def fake_role(db, user, production_id):
        return role
    monkeypatch.setattr("app.dependencies.get_user_role_for_production", fake_role)


def test_trigger_entity_extraction_blocked_for_reviewer(monkeypatch):
    _patch_role(monkeypatch, "reviewer")
    db = FakeSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            ingest_router.trigger_entity_extraction(
                production_id=1,
                background_tasks=BackgroundTasks(),
                db=db,
                user=FakeUser("u1"),
            )
        )
    assert exc.value.status_code == 403


def test_trigger_entity_extraction_falls_back_to_background_tasks(monkeypatch):
    _patch_role(monkeypatch, "manager")
    monkeypatch.setattr(task_service, "is_configured", lambda: False)
    audit_calls = []

    async def fake_log(*args, **kwargs):
        audit_calls.append((args, kwargs))
    monkeypatch.setattr(ingest_router, "log_action", fake_log)

    db = FakeSession()
    bg = BackgroundTasks()
    out = asyncio.run(
        ingest_router.trigger_entity_extraction(
            production_id=1,
            background_tasks=bg,
            db=db,
            user=FakeUser("u1"),
        )
    )
    assert out == {"status": "started"}
    assert len(bg.tasks) == 1
    assert len(audit_calls) == 1


def test_trigger_entity_extraction_enqueues_via_cloud_tasks(monkeypatch):
    _patch_role(monkeypatch, "manager")
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    enqueue_calls = []
    monkeypatch.setattr(task_service, "enqueue_pipeline", lambda production_id: enqueue_calls.append(production_id))
    audit_calls = []

    async def fake_log(*args, **kwargs):
        audit_calls.append((args, kwargs))
    monkeypatch.setattr(ingest_router, "log_action", fake_log)

    db = FakeSession()
    out = asyncio.run(
        ingest_router.trigger_entity_extraction(
            production_id=7,
            background_tasks=BackgroundTasks(),
            db=db,
            user=FakeUser("u1"),
        )
    )
    assert out == {"status": "enqueued"}
    assert enqueue_calls == [7]
    assert len(audit_calls) == 1
