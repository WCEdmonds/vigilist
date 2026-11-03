"""Haiku 5.5 per-document requests, the Message Batches wrapper, and the
pipeline's batched-stage contract (submit -> waiting -> poll -> apply -> done).

Fake Anthropic clients only; the real API is never called."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.claude_batch as claude_batch
import app.services.pipeline as pipeline
import app.services.tasks as tasks
from app.config import settings


# --- model + request options ------------------------------------------------------

def test_per_doc_model_is_haiku_5_5():
    from app.services import ai, entity_extraction
    assert ai.PER_DOC_MODEL == "claude-haiku-5-5" and entity_extraction.EXTRACTION_MODEL == "claude-haiku-5-5"


def _text_response(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.fixture
def captured_request(monkeypatch):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("system"):
            return _text_response('{"entities": [], "events": [], "relationships": []}')
        return _text_response("ACME memo re widgets")

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: client)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    return calls


def _assert_per_doc_kwargs(kwargs):
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["thinking"] == {"type": "disabled"}
    assert kwargs["output_config"] == {"effort": "low"}
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in kwargs


def test_per_doc_requests_disable_thinking_and_send_no_sampling(captured_request):
    from app.services import ai, entity_extraction
    assert asyncio.run(ai.generate_summary("ACME Corp memo about widgets.")) == "ACME memo re widgets"
    asyncio.run(ai.generate_title("ACME Corp memo about widgets."))
    asyncio.run(entity_extraction.extract_document_entities("ACME Corp memo about widgets."))
    assert len(captured_request) == 3
    for kwargs in captured_request:
        _assert_per_doc_kwargs(kwargs)


def test_request_param_builders_match_sync_calls():
    from app.services import ai, entity_extraction
    _assert_per_doc_kwargs(ai.summary_request_params("ACME text"))
    assert ai.summary_request_params("   ") is None
    params = entity_extraction.extraction_request_params("ACME text")
    _assert_per_doc_kwargs(params)
    assert params["system"] == entity_extraction.EXTRACTION_SYSTEM_PROMPT


# --- claude_batch -------------------------------------------------------------------

def _result(custom_id, kind, text=None):
    if kind == "succeeded":
        block = SimpleNamespace(type="text", text=text)
        res = SimpleNamespace(type="succeeded", message=SimpleNamespace(content=[block]))
    else:
        res = SimpleNamespace(type=kind)
    return SimpleNamespace(custom_id=custom_id, result=res)


class FakeBatches:
    def __init__(self, status="ended", results=()):
        self.status = status
        self._results = list(results)
        self.created = []

    async def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id="msgbatch_test")

    async def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    async def results(self, batch_id):
        items = list(self._results)

        async def gen():
            for item in items:
                yield item
        return gen()


def _patch_batches(monkeypatch, batches):
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    monkeypatch.setattr(claude_batch, "_client", lambda: client)


def test_submit_sends_custom_ids_and_params(monkeypatch):
    fb = FakeBatches()
    _patch_batches(monkeypatch, fb)
    batch_id = asyncio.run(claude_batch.submit([("doc-a", {"model": "m"}), ("doc-b", {"model": "m"})]))
    assert batch_id == "msgbatch_test"
    assert fb.created == [[{"custom_id": "doc-a", "params": {"model": "m"}},
                           {"custom_id": "doc-b", "params": {"model": "m"}}]]


def test_fetch_if_done_none_while_processing(monkeypatch):
    _patch_batches(monkeypatch, FakeBatches(status="in_progress"))
    assert asyncio.run(claude_batch.fetch_if_done("b1")) is None


def test_fetch_if_done_keys_by_custom_id_in_any_order(monkeypatch):
    results = [
        _result("doc-c", "succeeded", " third "),
        _result("doc-a", "succeeded", "first"),
        _result("doc-b", "errored"),
        _result("doc-d", "expired"),
        _result("doc-e", "canceled"),
    ]
    _patch_batches(monkeypatch, FakeBatches(results=results))
    out = asyncio.run(claude_batch.fetch_if_done("b1"))
    assert out == {"doc-a": "first", "doc-b": None, "doc-c": "third",
                   "doc-d": None, "doc-e": None}


# --- tasks.enqueue_batch_poll -------------------------------------------------------------

def test_enqueue_batch_poll_schedules_later(monkeypatch):
    sink = []

    class FakeTasksClient:
        def queue_path(self, *a):
            return "q"

        def create_task(self, parent, task):
            sink.append(task)

    monkeypatch.setattr(tasks.tasks_v2, "CloudTasksClient", FakeTasksClient)
    monkeypatch.setattr(tasks, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "cloud_run_service_url", "https://api.example")
    monkeypatch.setattr(settings, "cloud_tasks_worker_url", "https://worker.example")
    before = datetime.now(timezone.utc)
    tasks.enqueue_batch_poll(7, "summaries", 300)
    task = sink[0]
    assert task.http_request.url == "https://worker.example/api/ingest/poll-batch"
    import json
    assert json.loads(task.http_request.body) == {"production_id": 7, "stage": "summaries"}
    scheduled = task.schedule_time
    # proto-plus exposes Timestamp fields as an aware datetime
    delta = (scheduled - before).total_seconds()
    assert 295 <= delta <= 310
    # No delay -> no schedule_time.
    tasks.enqueue_batch_poll(7, "summaries", 0)
    assert not tasks.tasks_v2.Task.pb(sink[1]).HasField("schedule_time")


def test_constants():
    assert pipeline.BATCH_THRESHOLD_DOCS == 500
    assert pipeline.BATCH_POLL_SECONDS == 300


# --- pipeline batched stage -------------------------------------------------------------------

class FakeDoc:
    def __init__(self, n, text=None):
        self.id = uuid.UUID(int=n + 1)
        self.production_id = 1
        self.bates_begin = f"ACME-{n:06d}"
        self.text_content = text if text is not None else f"ACME document {n} about widgets."
        self.summary = None
        self.entities_extracted_at = None
        self.email_from = self.email_to = self.email_cc = self.email_bcc = None


class World:
    """In-memory production + documents behind pipeline.async_session."""

    def __init__(self, docs, status=None):
        self.prod = SimpleNamespace(id=1, ai_pipeline_status=status, ai_hold=False)
        self.docs = {d.id: d for d in docs}
        self.commits = 0

    def session(self):
        world = self

        class S:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

            async def get(self, model, key, **kw):
                if model.__name__ == "Production":
                    return world.prod if key == world.prod.id else None
                return world.docs.get(uuid.UUID(str(key)))

            async def commit(self):
                world.commits += 1

        return S()


def _install(monkeypatch, world, configured=True):
    monkeypatch.setattr(pipeline, "async_session", world.session)
    monkeypatch.setattr(tasks, "is_configured", lambda: configured)

    async def count_pending(production_id, stage):
        attr = "summary" if stage == "summaries" else "entities_extracted_at"
        return sum(1 for d in world.docs.values() if getattr(d, attr) is None and d.text_content)

    async def load_docs(production_id, stage, offset, limit):
        attr = "summary" if stage == "summaries" else "entities_extracted_at"
        pending = sorted((d for d in world.docs.values()
                          if getattr(d, attr) is None and d.text_content),
                         key=lambda d: d.bates_begin)
        return pending[offset:offset + limit]

    async def cas_stage(production_id, stage, prev, new):
        # Same contract as the conditional UPDATE: atomic within one event
        # loop step (no await between compare and set).
        status = dict(world.prod.ai_pipeline_status or {})
        if status.get(stage) != prev:
            return False
        status[stage] = new
        world.prod.ai_pipeline_status = status
        return True

    monkeypatch.setattr(pipeline, "_count_pending", count_pending)
    monkeypatch.setattr(pipeline, "_load_batch_docs", load_docs)
    monkeypatch.setattr(pipeline, "_cas_stage", cas_stage)
    polls, pipelines = [], []
    monkeypatch.setattr(tasks, "enqueue_batch_poll",
                        lambda pid, stage, delay_s: polls.append((pid, stage, delay_s)))
    monkeypatch.setattr(tasks, "enqueue_pipeline",
                        lambda pid, *a, **k: pipelines.append(pid))
    return polls, pipelines


def test_large_summary_run_submits_batch_and_waits(monkeypatch):
    world = World([FakeDoc(i) for i in range(600)], status={"clustering": "done"})
    polls, _ = _install(monkeypatch, world)
    submitted = []

    async def fake_submit(requests):
        submitted.append(requests)
        return "msgbatch_1"

    monkeypatch.setattr(claude_batch, "submit", fake_submit)

    async def must_not_run(pid):
        raise AssertionError("later stages wait for the batched stage")

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "entities", must_not_run)
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "timeline_review", must_not_run)
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "brief", must_not_run)

    asyncio.run(pipeline.run_ambient_pipeline(1))

    assert len(submitted) == 1 and len(submitted[0]) == 600
    ids = {cid for cid, _ in submitted[0]}
    assert ids == {str(d.id) for d in world.docs.values()}
    for _, params in submitted[0]:
        assert params["model"] == "claude-haiku-5-5"
        assert params["thinking"] == {"type": "disabled"}
    entry = world.prod.ai_pipeline_status["summaries"]
    assert entry["status"] == "waiting" and entry["batch_id"] == "msgbatch_1"
    assert entry["submitted_at"]
    assert polls == [(1, "summaries", pipeline.BATCH_POLL_SECONDS)]
    # No model call happened synchronously.
    assert all(d.summary is None for d in world.docs.values())


def test_waiting_stage_blocks_a_rerun(monkeypatch):
    status = {"clustering": "done",
              "summaries": {"status": "waiting", "batch_id": "b1",
                            "submitted_at": datetime.now(timezone.utc).isoformat()}}
    world = World([FakeDoc(i) for i in range(600)], status=status)
    _install(monkeypatch, world)
    monkeypatch.setattr(claude_batch, "submit", AsyncMock(side_effect=AssertionError("no resubmit")))
    asyncio.run(pipeline.run_ambient_pipeline(1, force=True))
    assert world.prod.ai_pipeline_status["summaries"]["batch_id"] == "b1"


def test_small_run_stays_synchronous(monkeypatch):
    world = World([FakeDoc(i) for i in range(500)], status={"clustering": "done"})
    _install(monkeypatch, world)
    monkeypatch.setattr(claude_batch, "submit", AsyncMock(side_effect=AssertionError("no batch")))
    assert asyncio.run(pipeline._maybe_submit_batch(1, "summaries")) is False


def test_batches_need_cloud_tasks(monkeypatch):
    # Without Cloud Tasks nothing could poll, so big runs stay synchronous.
    world = World([FakeDoc(i) for i in range(600)], status={"clustering": "done"})
    _install(monkeypatch, world, configured=False)
    monkeypatch.setattr(claude_batch, "submit", AsyncMock(side_effect=AssertionError("no batch")))
    assert asyncio.run(pipeline._maybe_submit_batch(1, "summaries")) is False


def _waiting(stage, batch_id="b1", more=False):
    return {"clustering": "done",
            stage: {"status": "waiting", "batch_id": batch_id, "more": more,
                    "submitted_at": datetime.now(timezone.utc).isoformat()}}


def test_poll_applies_results_and_marks_done(monkeypatch):
    docs = [FakeDoc(i) for i in range(3)]
    world = World(docs, status=_waiting("summaries"))
    polls, pipelines = _install(monkeypatch, world)

    async def fake_fetch(batch_id):
        assert batch_id == "b1"
        return {str(docs[2].id): "Summary C", str(docs[0].id): "Summary A",
                str(docs[1].id): None}

    monkeypatch.setattr(claude_batch, "fetch_if_done", fake_fetch)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert docs[0].summary == "Summary A"
    assert docs[2].summary == "Summary C"
    assert docs[1].summary is None  # errored: left for the next run
    assert world.prod.ai_pipeline_status["summaries"] == "done"
    assert polls == []
    assert pipelines == [1]  # pipeline continues with the later stages


def test_poll_applies_entity_results_through_shared_persistence(monkeypatch):
    docs = [FakeDoc(i) for i in range(2)]
    world = World(docs, status=_waiting("entities"))
    _install(monkeypatch, world)
    persisted = []

    async def fake_persist(db, production_id, document_id, text, parsed):
        persisted.append((document_id, [e["name"] for e in parsed["entities"]]))
        return {}

    import app.services.entity_extraction as ee
    monkeypatch.setattr(ee, "persist_extraction", fake_persist)
    ok = ('{"entities": [{"name": "ACME Corp", "type": "org", '
          '"surface_forms": ["ACME Corp"], "role": null, "emails": []}], '
          '"events": [], "relationships": []}')

    async def fake_fetch(batch_id):
        return {f"{docs[1].id}_0": ok, f"{docs[0].id}_0": None}

    monkeypatch.setattr(claude_batch, "fetch_if_done", fake_fetch)
    asyncio.run(pipeline.poll_batch_stage(1, "entities"))
    assert persisted == [(docs[1].id, ["ACME Corp"])]
    assert docs[1].entities_extracted_at is not None
    assert docs[0].entities_extracted_at is None
    assert world.prod.ai_pipeline_status["entities"] == "done"


def test_poll_with_more_pending_reruns_the_stage(monkeypatch):
    docs = [FakeDoc(i) for i in range(2)]
    world = World(docs, status=_waiting("summaries", more=True))
    _, pipelines = _install(monkeypatch, world)

    async def fake_fetch(batch_id):
        return {str(docs[0].id): "Summary A"}

    monkeypatch.setattr(claude_batch, "fetch_if_done", fake_fetch)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    # Not done yet: the next pipeline run submits the next slice.
    assert world.prod.ai_pipeline_status["summaries"] == "pending"
    assert pipelines == [1]


def test_poll_reschedules_while_processing(monkeypatch):
    world = World([FakeDoc(0)], status=_waiting("summaries"))
    polls, pipelines = _install(monkeypatch, world)
    monkeypatch.setattr(claude_batch, "fetch_if_done", AsyncMock(return_value=None))
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert polls == [(1, "summaries", 300)]
    assert world.prod.ai_pipeline_status["summaries"]["status"] == "waiting"
    assert pipelines == []


def test_poll_without_waiting_stage_is_a_noop(monkeypatch):
    world = World([FakeDoc(0)], status={"summaries": "done"})
    polls, pipelines = _install(monkeypatch, world)
    monkeypatch.setattr(claude_batch, "fetch_if_done",
                        AsyncMock(side_effect=AssertionError("nothing to poll")))
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert polls == [] and pipelines == []


def test_stale_waiting_stage_is_resubmitted(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=pipeline.BATCH_STALE_HOURS + 1)).isoformat()
    status = {"clustering": "done",
              "summaries": {"status": "waiting", "batch_id": "old", "submitted_at": old}}
    world = World([FakeDoc(i) for i in range(600)], status=status)
    _install(monkeypatch, world)
    monkeypatch.setattr(claude_batch, "submit", AsyncMock(return_value="new"))
    for stage in ("entities", "timeline_review", "brief"):
        monkeypatch.setitem(pipeline._STAGE_RUNNERS, stage, AsyncMock())
    asyncio.run(pipeline.run_ambient_pipeline(1))
    assert world.prod.ai_pipeline_status["summaries"]["batch_id"] == "new"


def test_poll_batch_endpoint_requires_stage(monkeypatch):
    import app.routers.ingest as ingest_router
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(ingest_router.poll_batch_handler({"production_id": 1}, None))
    seen = []

    async def fake_poll(pid, stage):
        seen.append((pid, stage))

    monkeypatch.setattr(pipeline, "poll_batch_stage", fake_poll)
    out = asyncio.run(ingest_router.poll_batch_handler({"production_id": 1, "stage": "entities"}, None))
    assert out["ok"] is True and seen == [(1, "entities")]


# --- fix round 1: no resubmit loop, single applier, multi-slice safety ---------------

def test_partially_errored_final_batch_marks_next_attempt_sync(monkeypatch):
    docs = [FakeDoc(i) for i in range(2)]
    world = World(docs, status=_waiting("summaries"))
    _install(monkeypatch, world)
    monkeypatch.setattr(claude_batch, "fetch_if_done", AsyncMock(
        return_value={str(docs[0].id): "Summary A", str(docs[1].id): None}))
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    status = world.prod.ai_pipeline_status
    assert status["summaries"] == "done"
    assert status[pipeline.NEXT_ATTEMPT_KEY] == {"summaries": "sync"}


def test_all_errored_batch_fails_without_resubmitting_then_runs_sync(monkeypatch):
    docs = [FakeDoc(i) for i in range(600)]
    world = World(docs, status=_waiting("summaries"))
    polls, pipelines = _install(monkeypatch, world)
    submit = AsyncMock(return_value="again")
    monkeypatch.setattr(claude_batch, "submit", submit)
    monkeypatch.setattr(claude_batch, "fetch_if_done", AsyncMock(
        return_value={str(d.id): None for d in docs}))

    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    status = world.prod.ai_pipeline_status
    assert status["summaries"] == "failed"
    assert "no request succeeded" in status["errors"]["summaries"]
    assert status[pipeline.NEXT_ATTEMPT_KEY] == {"summaries": "sync"}
    assert pipelines == [] and polls == []  # pipeline not auto-continued
    submit.assert_not_called()

    # The next (explicit) run handles the stage synchronously, once.
    sync_runs = []

    async def fake_sync(pid):
        sync_runs.append(pid)

    monkeypatch.setattr(pipeline, "_run_summaries_sync", fake_sync)
    for stage in ("entities", "timeline_review", "brief"):
        monkeypatch.setitem(pipeline._STAGE_RUNNERS, stage, AsyncMock())
    asyncio.run(pipeline.run_ambient_pipeline(1))
    submit.assert_not_called()
    assert sync_runs == [1]
    assert world.prod.ai_pipeline_status["summaries"] == "done"
    assert world.prod.ai_pipeline_status[pipeline.NEXT_ATTEMPT_KEY] == {}
    # Marker consumed: a later large run batches again.
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    submit.assert_called_once()


def test_concurrent_duplicate_polls_apply_once(monkeypatch):
    docs = [FakeDoc(i) for i in range(3)]
    world = World(docs, status=_waiting("summaries"))
    _, pipelines = _install(monkeypatch, world)

    async def slow_fetch(batch_id):
        await asyncio.sleep(0)  # both polls fetch before either claims
        return {str(d.id): f"Summary {i}" for i, d in enumerate(docs)}

    monkeypatch.setattr(claude_batch, "fetch_if_done", slow_fetch)
    applied = []
    real_apply = pipeline._apply_summary_results

    async def counting_apply(*a, **k):
        applied.append(1)
        return await real_apply(*a, **k)

    monkeypatch.setattr(pipeline, "_apply_summary_results", counting_apply)

    async def both():
        await asyncio.gather(pipeline.poll_batch_stage(1, "summaries"),
                             pipeline.poll_batch_stage(1, "summaries"))

    asyncio.run(both())
    assert applied == [1]
    assert pipelines == [1]
    assert world.prod.ai_pipeline_status["summaries"] == "done"


def test_poll_ignores_a_live_applying_claim(monkeypatch):
    entry = {"status": "applying", "batch_id": "b1", "more": False,
             "submitted_at": datetime.now(timezone.utc).isoformat(),
             "applying_at": datetime.now(timezone.utc).isoformat()}
    world = World([FakeDoc(0)], status={"summaries": entry})
    _install(monkeypatch, world)
    monkeypatch.setattr(claude_batch, "fetch_if_done",
                        AsyncMock(side_effect=AssertionError("claimed elsewhere")))
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert world.prod.ai_pipeline_status["summaries"] == entry


def test_cas_stage_issues_conditional_update(monkeypatch):
    seen = {}

    class Res:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    def session_with(row):
        class S:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

            async def execute(self, stmt, params=None):
                seen["sql"], seen["params"] = str(stmt), params
                return Res(row)

            async def commit(self): pass
        return S()

    import json
    prev = {"status": "waiting", "batch_id": "b1"}
    new = {"status": "applying", "batch_id": "b1"}
    monkeypatch.setattr(pipeline, "async_session", lambda: session_with((1,)))
    assert asyncio.run(pipeline._cas_stage(1, "summaries", prev, new)) is True
    assert "WHERE id = :pid AND ai_pipeline_status -> cast(:stage as text) = cast(:prev as jsonb)" in seen["sql"]
    assert json.loads(seen["params"]["prev"]) == prev and json.loads(seen["params"]["new"]) == new
    monkeypatch.setattr(pipeline, "async_session", lambda: session_with(None))
    assert asyncio.run(pipeline._cas_stage(1, "summaries", prev, new)) is False


def test_multi_slice_doc_with_a_failed_slice_is_not_applied(monkeypatch):
    from app.services import entity_extraction as ee
    long_text = "ACME widget ledger line. " * 8_000  # > one 140K-char slice
    assert len(ee.slice_text(long_text)) == 2
    partial = FakeDoc(0, text=long_text)
    complete = FakeDoc(1, text=long_text)
    world = World([partial, complete])
    _install(monkeypatch, world)
    persisted = []

    async def fake_persist(db, production_id, document_id, text, parsed):
        persisted.append((document_id, sorted(e["name"] for e in parsed["entities"])))
        return {}

    monkeypatch.setattr(ee, "persist_extraction", fake_persist)

    def ent(name):
        return ('{"entities": [{"name": "%s", "type": "org", "surface_forms": ["%s"], '
                '"role": null, "emails": []}], "events": [], "relationships": []}' % (name, name))

    results = {
        f"{partial.id}_0": ent("ACME Corp"), f"{partial.id}_1": None,
        f"{complete.id}_1": ent("Widget LLC"), f"{complete.id}_0": ent("ACME Corp"),
    }
    usable, unprocessed, finished = asyncio.run(
        pipeline._apply_entity_results(1, results, deadline=float("inf")))
    assert (usable, unprocessed, finished) == (1, 1, True)
    assert persisted == [(complete.id, ["ACME Corp", "Widget LLC"])]
    assert complete.entities_extracted_at is not None
    assert partial.entities_extracted_at is None
