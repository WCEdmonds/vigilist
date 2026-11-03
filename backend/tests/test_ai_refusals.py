"""I8: a refusal (stop_reason "refusal") is recorded so the document is not
resubmitted on every run. Fake Anthropic clients only."""

import asyncio
from types import SimpleNamespace

import app.services.claude_batch as claude_batch
import app.services.pipeline as pipeline
from app.config import settings
from app.services import ai
from tests.test_claude_batch import FakeBatches, FakeDoc, World, _install, _patch_batches, _waiting


def _refusal_response():
    return SimpleNamespace(content=[], stop_reason="refusal")


def _client_returning(response):
    async def create(**kwargs):
        return response

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_refused_marker_is_falsy_and_distinct():
    assert not ai.REFUSED and ai.is_refusal(ai.REFUSED)
    assert not ai.is_refusal("") and not ai.is_refusal(None) and not ai.is_refusal("text")


def test_sync_summary_and_title_return_the_refusal_marker(monkeypatch):
    monkeypatch.setattr(ai, "_get_client", lambda: _client_returning(_refusal_response()))
    assert ai.is_refusal(asyncio.run(ai.generate_summary("ACME memo")))
    assert ai.is_refusal(asyncio.run(ai.generate_title("ACME memo")))


def test_sync_summaries_record_an_empty_summary_and_stop(monkeypatch):
    docs = [FakeDoc(0), FakeDoc(1)]
    world = World(docs, status={})
    _install(monkeypatch, world)

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    def session():
        s = world.session()

        async def execute(stmt, params=None):
            pending = [d for d in docs if d.summary is None]
            return Rows([(d.id, d.text_content) for d in pending])

        async def add(*a):
            return None

        s.execute = execute
        s.add = lambda obj: None
        return s

    monkeypatch.setattr(pipeline, "async_session", session)

    async def fake_batch(texts):
        return {texts[0][0]: ai.REFUSED, **{d: "Summary" for d, _ in texts[1:]}}

    monkeypatch.setattr(pipeline, "generate_summaries_batch", fake_batch)
    asyncio.run(pipeline._run_summaries_sync(1))
    assert docs[0].summary == "" and docs[0].metadata_["ai_refused"] == ["summaries"]
    assert docs[1].summary == "Summary"


def test_batch_results_map_refusals_to_the_marker(monkeypatch):
    refused = SimpleNamespace(custom_id="a", result=SimpleNamespace(
        type="succeeded", message=SimpleNamespace(content=[], stop_reason="refusal")))
    ok = SimpleNamespace(custom_id="b", result=SimpleNamespace(
        type="succeeded", message=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Summary B")], stop_reason="end_turn")))
    _patch_batches(monkeypatch, FakeBatches(results=[refused, ok]))
    out = asyncio.run(claude_batch.fetch_if_done("b1"))
    assert ai.is_refusal(out["a"]) and out["b"] == "Summary B"


def test_batched_summary_refusal_is_recorded_not_resubmitted(monkeypatch):
    docs = [FakeDoc(0), FakeDoc(1)]
    world = World(docs, status=_waiting("summaries"))
    _install(monkeypatch, world)

    async def fake_fetch(batch_id):
        return {str(docs[0].id): ai.REFUSED, str(docs[1].id): "Summary B"}

    monkeypatch.setattr(claude_batch, "fetch_if_done", fake_fetch)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert docs[0].summary == "" and docs[0].metadata_["ai_refused"] == ["summaries"]
    assert docs[1].summary == "Summary B"
    # Not an unprocessed item: no synchronous retry is scheduled.
    assert world.prod.ai_pipeline_status["summaries"] == "done"
    assert not (world.prod.ai_pipeline_status.get(pipeline.NEXT_ATTEMPT_KEY) or {}).get("summaries")


def test_batched_entity_refusal_marks_the_document_extracted(monkeypatch):
    import app.services.entity_extraction as ee

    docs = [FakeDoc(0)]
    world = World(docs, status=_waiting("entities"))
    _install(monkeypatch, world)
    persisted = []

    async def fake_persist(db, production_id, document_id, text, parsed):
        persisted.append(document_id)
        return {}

    monkeypatch.setattr(ee, "persist_extraction", fake_persist)

    async def fake_fetch(batch_id):
        return {f"{docs[0].id}_0": ai.REFUSED}

    monkeypatch.setattr(claude_batch, "fetch_if_done", fake_fetch)
    asyncio.run(pipeline.poll_batch_stage(1, "entities"))
    assert docs[0].entities_extracted_at is not None
    assert docs[0].metadata_["ai_refused"] == ["entities"]
    assert world.prod.ai_pipeline_status["entities"] == "done"


def test_sync_entity_refusal_marks_the_document_extracted(monkeypatch):
    import anthropic

    import app.services.entity_extraction as ee

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: _client_returning(_refusal_response()))
    parsed = asyncio.run(ee.extract_document_entities("ACME memo about widgets."))
    assert parsed["refused"] is True and parsed["entities"] == []

    async def fake_persist(db, production_id, document_id, text, parsed):
        return {}

    monkeypatch.setattr(ee, "persist_extraction", fake_persist)
    doc = FakeDoc(0)
    assert asyncio.run(pipeline._extract_one_document(SimpleNamespace(), doc)) is True
    assert doc.entities_extracted_at is not None
    assert doc.metadata_["ai_refused"] == ["entities"]


def test_refused_titles_are_not_requested_again(monkeypatch):
    import app.services.ingest as ingest_mod
    from tests.fakes import FakeResult, FakeSession

    refused = SimpleNamespace(id="d1", title=None, text_content="x", metadata_={})
    marked = SimpleNamespace(id="d2", title=None, text_content="y", metadata_={"ai_refused": ["titles"]})
    asked = []

    async def fake_titles(texts):
        asked.append([d for d, _ in texts])
        return {d: ai.REFUSED for d, _ in texts}

    monkeypatch.setattr(ingest_mod, "generate_titles_batch", fake_titles)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    db = FakeSession(responders=[("FROM documents", FakeResult(items=[refused, marked]))])
    errors = []
    asyncio.run(ingest_mod.generate_missing_titles(db, 1, errors))
    assert asked == [["d1"]]
    assert refused.metadata_["ai_refused"] == ["titles"] and refused.title is None
    assert errors == []
