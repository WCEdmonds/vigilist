"""Entity extraction stage: batching, idempotency marker, per-doc failure
isolation, stop-on-fully-failed-batch (mirrors the summaries stage contract)."""

import asyncio
import uuid
from datetime import datetime

import app.services.pipeline as pipeline


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
