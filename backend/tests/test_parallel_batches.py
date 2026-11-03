"""Parallel Message Batch rounds, concurrent summaries + entities, the
legacy single-batch transition, and no AI hold while analysis is running.

Fake Anthropic batch client and in-memory production only; the real API is
never called. Synthetic ACME data only."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import app.services.claude_batch as claude_batch
import app.services.pipeline as pipeline
from tests.test_claude_batch import FakeDoc, World, _install


def _now(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


class FakeBatchAPI:
    """Submits get sequential ids; each batch answers for its own custom ids
    once ``finish`` has been called for it."""

    def __init__(self, fail_submit_at=None):
        self.submitted: dict[str, list[tuple[str, dict]]] = {}
        self.ended: set[str] = set()
        self.overrides: dict[str, dict] = {}
        self.fail_submit_at = fail_submit_at
        self.fetches: list[str] = []

    async def submit(self, requests):
        n = len(self.submitted) + 1
        if self.fail_submit_at is not None and n >= self.fail_submit_at:
            raise RuntimeError("batch submit failed")
        bid = f"msgbatch_{n}"
        self.submitted[bid] = list(requests)
        return bid

    def finish(self, *ids, results=None):
        for bid in ids:
            self.ended.add(bid)
            if results is not None:
                self.overrides[bid] = results

    async def fetch_if_done(self, batch_id):
        self.fetches.append(batch_id)
        await asyncio.sleep(0)  # let concurrent polls interleave
        if batch_id not in self.ended:
            return None
        if batch_id in self.overrides:
            return self.overrides[batch_id]
        out = {}
        for cid, _ in self.submitted.get(batch_id, []):
            if "_" in cid:
                out[cid] = '{"entities": [], "events": [], "relationships": []}'
            else:
                out[cid] = f"Summary of {cid[-4:]}"
        return out


@pytest.fixture
def env(monkeypatch):
    def make(n_docs=2_500, status=None, max_requests=1_000, max_inflight=10, **api_kw):
        world = World([FakeDoc(i) for i in range(n_docs)],
                      status={"clustering": "done"} if status is None else status)
        polls, pipelines = _install(monkeypatch, world)
        api = FakeBatchAPI(**api_kw)
        monkeypatch.setattr(claude_batch, "submit", api.submit)
        monkeypatch.setattr(claude_batch, "fetch_if_done", api.fetch_if_done)
        monkeypatch.setattr(pipeline, "BATCH_MAX_REQUESTS", max_requests)
        monkeypatch.setattr(pipeline, "MAX_INFLIGHT_BATCHES", max_inflight)

        async def fake_persist(db, production_id, document_id, text, parsed):
            return {}

        import app.services.entity_extraction as ee
        monkeypatch.setattr(ee, "persist_extraction", fake_persist)
        final_calls = []
        for stage in ("timeline_review", "brief"):
            async def runner(pid, _s=stage):
                await asyncio.sleep(0)
                final_calls.append(_s)
            monkeypatch.setitem(pipeline._STAGE_RUNNERS, stage, runner)
        return world, api, polls, pipelines, final_calls
    return make


def _entry(world, stage):
    return world.prod.ai_pipeline_status[stage]


def _clear_poll_schedule(world):
    """Pretend the scheduled poll's time has come (no wall-clock sleeping)."""
    for stage in pipeline.BATCHED_STAGES:
        entry = world.prod.ai_pipeline_status.get(stage)
        if isinstance(entry, dict):
            entry.pop("next_poll_at", None)


# --- 1. every round of a stage submitted at once ------------------------------------

def test_large_stage_submits_every_round_at_once(env, monkeypatch):
    world, api, polls, _, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    entry = _entry(world, "summaries")
    assert entry["status"] == "waiting"
    assert [b["id"] for b in entry["batches"]] == ["msgbatch_1", "msgbatch_2", "msgbatch_3"]
    assert [b["doc_count"] for b in entry["batches"]] == [1000, 1000, 500]
    assert all(b["state"] == "processing" and b["submitted_at"] for b in entry["batches"])
    assert entry["exhausted"] is True
    ids = [cid for reqs in api.submitted.values() for cid, _ in reqs]
    assert len(ids) == len(set(ids)) == 2_500  # disjoint rounds covering every doc
    assert set(ids) == {str(d.id) for d in world.docs.values()}
    assert polls == [(1, "summaries", pipeline.BATCH_POLL_SECONDS)]  # one poll chain


def test_rounds_also_split_on_request_bytes(env, monkeypatch):
    world, api, _, _, _ = env(n_docs=600, max_requests=10_000)
    from app.services.ai import summary_request_params
    one = len(json.dumps(summary_request_params(next(iter(world.docs.values())).text_content)))
    monkeypatch.setattr(pipeline, "BATCH_MAX_BYTES", one * 250)
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    counts = [b["doc_count"] for b in _entry(world, "summaries")["batches"]]
    assert sum(counts) == 600 and len(counts) >= 3 and max(counts) <= 250


def test_inflight_cap_defers_rounds_until_earlier_ones_finish(env):
    world, api, polls, pipelines, _ = env(max_inflight=2)
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    entry = _entry(world, "summaries")
    assert [b["id"] for b in entry["batches"]] == ["msgbatch_1", "msgbatch_2"]
    assert entry["exhausted"] is False

    api.finish("msgbatch_1")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    entry = _entry(world, "summaries")
    states = {b["id"]: b["state"] for b in entry["batches"]}
    assert states == {"msgbatch_1": "applied", "msgbatch_2": "processing",
                      "msgbatch_3": "processing"}
    assert entry["exhausted"] is True
    assert pipelines == []

    api.finish("msgbatch_2", "msgbatch_3")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert _entry(world, "summaries") == "done"
    assert all(d.summary for d in world.docs.values())
    assert pipelines == [1]


def test_failed_later_submit_keeps_submitted_rounds_and_tops_up_later(env):
    world, api, polls, _, _ = env(fail_submit_at=2)
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    entry = _entry(world, "summaries")
    assert [b["id"] for b in entry["batches"]] == ["msgbatch_1"]
    assert entry["exhausted"] is False and "submitter" not in entry

    api.fail_submit_at = None  # queue has room again
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    entry = _entry(world, "summaries")
    assert [b["id"] for b in entry["batches"]] == ["msgbatch_1", "msgbatch_2", "msgbatch_3"]
    ids = [cid for reqs in api.submitted.values() for cid, _ in reqs]
    assert len(ids) == len(set(ids)) == 2_500


def test_first_submit_failure_fails_the_stage(env):
    world, api, _, _, _ = env(fail_submit_at=1)
    asyncio.run(pipeline.run_ambient_pipeline(1))
    status = world.prod.ai_pipeline_status
    assert status["summaries"] == "failed" and "submit failed" in status["errors"]["summaries"]


# --- poll ----------------------------------------------------------------------------

def test_poll_applies_finished_batches_and_keeps_waiting_on_the_rest(env):
    world, api, polls, pipelines, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    polls.clear()
    api.finish("msgbatch_2")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    states = [b["state"] for b in _entry(world, "summaries")["batches"]]
    assert states == ["processing", "applied", "processing"]
    assert sum(1 for d in world.docs.values() if d.summary) == 1000
    assert polls == [(1, "summaries", 300)]
    assert pipelines == []


def test_poll_dedups_its_own_chain(env):
    world, api, polls, _, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    polls.clear()
    # A duplicate poll right after the scheduled one does not start a second chain.
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert polls == []
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert polls == [(1, "summaries", 300)]


def test_stage_done_when_every_batch_applied_and_continues_once(env):
    world, api, polls, pipelines, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    api.finish("msgbatch_1", "msgbatch_2", "msgbatch_3")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert _entry(world, "summaries") == "done"
    assert world.prod.ai_pipeline_status[pipeline.NEXT_ATTEMPT_KEY] == {}
    assert pipelines == [1]
    # A late duplicate poll does nothing.
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert pipelines == [1]


def test_partial_failures_set_the_sync_marker(env):
    world, api, _, pipelines, _ = env(n_docs=1_200)
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    first = {cid: "S" for cid, _ in api.submitted["msgbatch_1"]}
    first[next(iter(first))] = None
    api.finish("msgbatch_1", results=first)
    api.finish("msgbatch_2")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    status = world.prod.ai_pipeline_status
    assert status["summaries"] == "done"
    assert status[pipeline.NEXT_ATTEMPT_KEY] == {"summaries": "sync"}
    assert pipelines == [1]


def test_all_failed_batch_fails_stage_after_the_others_and_does_not_continue(env):
    world, api, polls, pipelines, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    api.finish("msgbatch_2", results={cid: None for cid, _ in api.submitted["msgbatch_2"]})
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    # Still waiting on the other two: their results are already paid for.
    assert _entry(world, "summaries")["status"] == "waiting"
    api.finish("msgbatch_1", "msgbatch_3")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    status = world.prod.ai_pipeline_status
    assert status["summaries"] == "failed"
    assert "msgbatch_2" in status["errors"]["summaries"]
    assert "no request succeeded" in status["errors"]["summaries"]
    assert status[pipeline.NEXT_ATTEMPT_KEY] == {"summaries": "sync"}
    assert pipelines == []
    assert sum(1 for d in world.docs.values() if d.summary) == 1500


def test_all_failed_batch_stops_topping_up(env):
    world, api, _, _, _ = env(max_inflight=1)
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    api.finish("msgbatch_1", results={cid: None for cid, _ in api.submitted["msgbatch_1"]})
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert list(api.submitted) == ["msgbatch_1"]
    assert world.prod.ai_pipeline_status["summaries"] == "failed"


def test_duplicate_concurrent_polls_apply_each_batch_once(env, monkeypatch):
    world, api, _, pipelines, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    api.finish("msgbatch_1", "msgbatch_2", "msgbatch_3")
    _clear_poll_schedule(world)
    applied = []
    real = pipeline._apply_summary_results

    async def counting(production_id, results, deadline):
        applied.append(next(iter(results)))
        await asyncio.sleep(0)
        return await real(production_id, results, deadline)

    monkeypatch.setattr(pipeline, "_apply_summary_results", counting)

    async def both():
        await asyncio.gather(pipeline.poll_batch_stage(1, "summaries"),
                             pipeline.poll_batch_stage(1, "summaries"))

    asyncio.run(both())
    assert len(applied) == 3 and len(set(applied)) == 3  # each batch exactly once
    assert _entry(world, "summaries") == "done"
    assert pipelines == [1]  # finalized once


def test_two_polls_may_apply_different_batches(env, monkeypatch):
    """Claims are per batch: a live claim on one batch doesn't block another."""
    world, api, _, _, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    entry = world.prod.ai_pipeline_status["summaries"]
    entry["batches"][0]["state"] = "applying"
    entry["batches"][0]["applying_at"] = _now()
    entry["status"] = "applying"
    api.finish("msgbatch_1", "msgbatch_2")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    states = [b["state"] for b in _entry(world, "summaries")["batches"]]
    assert states == ["applying", "applied", "processing"]
    assert "msgbatch_1" not in api.fetches  # the live claim is left alone


def test_stale_batch_claim_is_taken_over(env):
    world, api, _, _, _ = env(n_docs=600)
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    entry = world.prod.ai_pipeline_status["summaries"]
    entry["batches"][0]["state"] = "applying"
    entry["batches"][0]["applying_at"] = _now(minutes=-(pipeline.BATCH_APPLY_STALE_MINUTES + 1))
    api.finish("msgbatch_1")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert _entry(world, "summaries") == "done"


def test_out_of_budget_releases_the_batch_and_repolls_now(env, monkeypatch):
    world, api, polls, _, _ = env(n_docs=600)
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    polls.clear()
    api.finish("msgbatch_1")
    monkeypatch.setattr(pipeline, "BATCH_APPLY_BUDGET_S", -1)
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    batch = _entry(world, "summaries")["batches"][0]
    assert batch["state"] == "processing" and "applying_at" not in batch
    assert polls == [(1, "summaries", 0)]


def test_parallel_entry_is_a_live_wait():
    entry = {"status": "waiting", "submitted_at": _now(), "exhausted": True,
             "batches": [{"id": "b1", "state": "processing", "submitted_at": _now(),
                          "doc_count": 1}]}
    assert pipeline._is_live_wait(entry) is True
    old = dict(entry, submitted_at=_now(hours=-(pipeline.BATCH_STALE_HOURS + 1)))
    assert pipeline._is_live_wait(old) is False
    applying = dict(old, status="applying", batches=[
        {"id": "b1", "state": "applying", "applying_at": _now(), "submitted_at": _now(),
         "doc_count": 1}])
    assert pipeline._is_live_wait(applying) is True
    from app.routers.productions import _is_actively_running
    assert _is_actively_running({"entities": entry}) is True


# --- 2. summaries and entities in parallel --------------------------------------------

def test_one_run_submits_summaries_and_entities_and_waits_on_both(env):
    world, api, polls, _, final_calls = env()
    asyncio.run(pipeline.run_ambient_pipeline(1))
    status = world.prod.ai_pipeline_status
    assert status["summaries"]["status"] == "waiting"
    assert status["entities"]["status"] == "waiting"
    assert sorted(polls) == [(1, "entities", 300), (1, "summaries", 300)]
    assert final_calls == []
    # Entities requests carry the slice suffix; summaries don't.
    ent_ids = [b["id"] for b in status["entities"]["batches"]]
    assert all("_" in cid for bid in ent_ids for cid, _ in api.submitted[bid])


def test_finishing_one_stage_does_not_resubmit_the_other_or_run_final_stages(env):
    world, api, polls, pipelines, final_calls = env()
    asyncio.run(pipeline.run_ambient_pipeline(1))
    ent_before = list(_entry(world, "entities")["batches"])
    n_submitted = len(api.submitted)
    for bid in [b["id"] for b in _entry(world, "summaries")["batches"]]:
        api.finish(bid)
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert pipelines == [1]
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))  # the continuation
    assert len(api.submitted) == n_submitted
    assert _entry(world, "entities")["batches"] == ent_before
    assert final_calls == []


def test_both_stages_finishing_at_once_run_final_stages_exactly_once(env):
    world, api, polls, pipelines, final_calls = env()
    asyncio.run(pipeline.run_ambient_pipeline(1))
    for stage in ("summaries", "entities"):
        for bid in [b["id"] for b in _entry(world, stage)["batches"]]:
            api.finish(bid)
    _clear_poll_schedule(world)

    async def both_polls():
        await asyncio.gather(pipeline.poll_batch_stage(1, "summaries"),
                             pipeline.poll_batch_stage(1, "entities"))

    asyncio.run(both_polls())
    assert pipelines == [1, 1]  # each stage continues

    async def both_runs():
        await asyncio.gather(pipeline.run_ambient_pipeline(1, resume=True),
                             pipeline.run_ambient_pipeline(1, resume=True))

    asyncio.run(both_runs())
    assert final_calls == ["timeline_review", "brief"]
    # And a third, late duplicate continuation does nothing.
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))
    assert final_calls == ["timeline_review", "brief"]
    status = world.prod.ai_pipeline_status
    assert status["timeline_review"] == "done" and status["brief"] == "done"


def test_small_stages_run_synchronously_then_final_stages(env, monkeypatch):
    world, api, polls, _, final_calls = env(n_docs=10)
    order = []
    for stage in ("clustering", "summaries", "entities"):
        async def runner(pid, _s=stage):
            order.append(_s)
        monkeypatch.setitem(pipeline._STAGE_RUNNERS, stage, runner)
    world.prod.ai_pipeline_status = None
    asyncio.run(pipeline.run_ambient_pipeline(1))
    assert order == ["clustering", "summaries", "entities"]
    assert final_calls == ["timeline_review", "brief"]
    status = world.prod.ai_pipeline_status
    assert all(status[s] == "done" for s in pipeline.STAGES)
    # A later forced run runs everything again, final stages included.
    asyncio.run(pipeline.run_ambient_pipeline(1, force=True))
    assert final_calls == ["timeline_review", "brief"] * 2


def test_resume_does_not_restart_a_failed_per_doc_stage(env, monkeypatch):
    status = {"clustering": "done", "summaries": "failed", "entities": "done",
              "errors": {"summaries": "batch b: no request succeeded"},
              pipeline.NEXT_ATTEMPT_KEY: {"summaries": "sync"}}
    world, api, _, _, final_calls = env(status=status)
    sync = AsyncMock()
    monkeypatch.setattr(pipeline, "_run_summaries_sync", sync)
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))
    sync.assert_not_called()
    assert api.submitted == {}
    # The all-failed batch blocks the final stages, as before parallel stages.
    assert final_calls == []
    # An explicit run retries it synchronously, then continues.
    asyncio.run(pipeline.run_ambient_pipeline(1))
    sync.assert_called_once()
    assert final_calls == ["timeline_review", "brief"]


def test_exception_failed_stage_does_not_block_final_stages(env, monkeypatch):
    world, api, _, _, final_calls = env(n_docs=10)

    async def boom(pid):
        raise RuntimeError("model down")

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "summaries", boom)
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "entities", AsyncMock())
    asyncio.run(pipeline.run_ambient_pipeline(1))
    assert world.prod.ai_pipeline_status["summaries"] == "failed"
    assert final_calls == ["timeline_review", "brief"]


# --- backward compatibility: a stage stored in the old single-batch shape ------------

def _legacy(batch_id, more, **extra):
    return {"status": "waiting", "batch_id": batch_id, "more": more,
            "submitted_at": _now(minutes=-20), **extra}


def test_legacy_live_wait_is_not_resubmitted_by_a_run(env):
    status = {"clustering": "done", "summaries": _legacy("msgbatch_old", True)}
    world, api, _, _, final_calls = env(status=status)
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))
    assert _entry(world, "summaries")["batch_id"] == "msgbatch_old"
    assert not any(cid.count("_") == 0 for reqs in api.submitted.values() for cid, _ in reqs)
    assert final_calls == []


def test_legacy_mid_run_transition_to_parallel_shape(env):
    """Production 16's shape: summaries waits on round 2 of ~6 (``more``),
    entities has not started. The old batch is finished and applied, then
    the remaining documents go out in the parallel shape, entities alongside;
    the run then completes normally."""
    status = {"clustering": "done", "summaries": _legacy("msgbatch_old", True),
              "errors": {}, "updated_at": _now(minutes=-20)}
    world, api, polls, pipelines, final_calls = env(n_docs=3_500, status=status)
    docs_sorted = sorted(world.docs.values(), key=lambda d: d.bates_begin)
    # Round 1 was applied before the deploy; round 2 is in flight.
    for d in docs_sorted[:1000]:
        d.summary = "earlier round"
    round2 = docs_sorted[1000:2000]
    api.finish("msgbatch_old", results={str(d.id): f"R2 {d.bates_begin}" for d in round2})

    # The old poll chain keeps polling the old batch and applies it.
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert all(d.summary.startswith("R2 ") for d in round2)
    assert world.prod.ai_pipeline_status["summaries"] == "pending"
    assert pipelines == [1]
    assert polls == []  # the legacy chain ends here

    # The continuation submits the rest in the new shape, entities in parallel.
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))
    s_entry, e_entry = _entry(world, "summaries"), _entry(world, "entities")
    assert "batch_id" not in s_entry and s_entry["status"] == "waiting"
    assert [b["doc_count"] for b in s_entry["batches"]] == [1000, 500]
    assert sum(b["doc_count"] for b in e_entry["batches"]) == 3_500
    sum_ids = {cid for b in s_entry["batches"] for cid, _ in api.submitted[b["id"]]}
    assert sum_ids == {str(d.id) for d in docs_sorted[2000:]}  # nothing re-sent
    assert sorted(polls) == [(1, "entities", 300), (1, "summaries", 300)]
    assert final_calls == []

    # Both stages finish; the final stages run once.
    for stage in ("summaries", "entities"):
        for b in _entry(world, stage)["batches"]:
            api.finish(b["id"])
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))
    assert final_calls == []
    asyncio.run(pipeline.poll_batch_stage(1, "entities"))
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))
    assert final_calls == ["timeline_review", "brief"]
    assert all(d.summary for d in world.docs.values())
    assert all(d.entities_extracted_at for d in world.docs.values())


def test_legacy_final_round_finishes_done_without_new_submits(env):
    status = {"clustering": "done", "summaries": _legacy("msgbatch_old", False),
              "entities": "done"}
    world, api, _, pipelines, final_calls = env(n_docs=3, status=status)
    api.finish("msgbatch_old", results={str(d.id): "S" for d in world.docs.values()})
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert world.prod.ai_pipeline_status["summaries"] == "done"
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))
    assert api.submitted == {}
    assert final_calls == ["timeline_review", "brief"]


def test_legacy_applying_claim_still_respected(env):
    entry = _legacy("msgbatch_old", True, status="applying", applying_at=_now())
    world, api, _, _, _ = env(n_docs=3, status={"summaries": entry})
    api.finish("msgbatch_old")
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert _entry(world, "summaries") == entry


# --- 3. no AI hold while a requested analysis is running ------------------------------

def test_analysis_in_progress_detection():
    now = _now()
    assert pipeline.analysis_in_progress(None) is False
    assert pipeline.analysis_in_progress({"summaries": "done", "updated_at": now}) is False
    assert pipeline.analysis_in_progress({"entities": "running", "updated_at": now}) is True
    assert pipeline.analysis_in_progress({"timeline_review": "queued", "updated_at": now}) is True
    assert pipeline.analysis_in_progress(
        {"summaries": {"status": "waiting", "batches": [], "submitted_at": now}}) is True
    stale = _now(hours=-3)
    assert pipeline.analysis_in_progress({"entities": "running", "updated_at": stale}) is False


# --- review fixes ---------------------------------------------------------------------

def _sdk_error(kind):
    import anthropic
    import httpx
    status = {"RateLimitError": 429, "OverloadedError": 529, "BadRequestError": 400}[kind]
    resp = httpx.Response(status, request=httpx.Request("POST", "https://api.example/v1/messages/batches"))
    return getattr(anthropic, kind)(f"{kind} from batches.create", response=resp, body=None)


def test_inflight_cap_is_four():
    assert pipeline.MAX_INFLIGHT_BATCHES == 4


def test_resume_does_not_rerun_a_failed_clustering(env, monkeypatch):
    status = {"clustering": "failed", "summaries": "pending", "entities": "pending"}
    world, api, _, _, _ = env(status=status)
    clustering = AsyncMock()
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "clustering", clustering)
    asyncio.run(pipeline.run_ambient_pipeline(1, resume=True))
    clustering.assert_not_called()
    assert world.prod.ai_pipeline_status["clustering"] == "failed"
    # The per-doc stages still go out.
    assert _entry(world, "summaries")["status"] == "waiting"
    assert _entry(world, "entities")["status"] == "waiting"
    # An explicit run retries clustering.
    world.prod.ai_pipeline_status["summaries"] = "done"
    world.prod.ai_pipeline_status["entities"] = "done"
    asyncio.run(pipeline.run_ambient_pipeline(1))
    clustering.assert_called_once()


@pytest.mark.parametrize("kind", ["RateLimitError", "OverloadedError"])
def test_first_round_refusal_waits_for_the_poll_instead_of_failing(env, monkeypatch, kind):
    world, api, polls, _, _ = env()
    err = _sdk_error(kind)
    real_submit = api.submit

    async def refuse(requests):
        raise err

    monkeypatch.setattr(claude_batch, "submit", refuse)
    asyncio.run(pipeline.run_ambient_pipeline(1))
    for stage in ("summaries", "entities"):
        entry = _entry(world, stage)
        assert entry["status"] == "waiting" and entry["batches"] == []
        assert entry["exhausted"] is False and "submitter" not in entry
    assert sorted(polls) == [(1, "entities", 300), (1, "summaries", 300)]
    assert "summaries" not in world.prod.ai_pipeline_status.get("errors", {})

    # Queue has room again: the poll submits the rounds.
    monkeypatch.setattr(claude_batch, "submit", real_submit)
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert sum(b["doc_count"] for b in _entry(world, "summaries")["batches"]) == 2_500


def test_first_round_non_retryable_error_fails_the_stage(env, monkeypatch):
    world, api, _, _, _ = env()
    err = _sdk_error("BadRequestError")

    async def bad(requests):
        raise err

    monkeypatch.setattr(claude_batch, "submit", bad)
    asyncio.run(pipeline.run_ambient_pipeline(1))
    assert world.prod.ai_pipeline_status["summaries"] == "failed"


def test_run_stops_submitting_when_its_budget_is_nearly_spent(env, monkeypatch):
    world, api, polls, _, _ = env()
    monkeypatch.setattr(pipeline, "RUN_BUDGET_S", pipeline.BATCH_SUBMIT_MIN_REMAINING_S - 1)
    asyncio.run(pipeline.run_ambient_pipeline(1))
    assert api.submitted == {}
    assert _entry(world, "summaries")["batches"] == []
    assert sorted(polls) == [(1, "entities", 300), (1, "summaries", 300)]
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert len(_entry(world, "summaries")["batches"]) == 3


def test_run_shares_one_deadline_across_both_stages(env, monkeypatch):
    world, api, _, _, _ = env()
    clock = {"t": 1000.0}
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock["t"])
    real = api.submit

    async def slow_submit(requests):
        clock["t"] += 400  # each round eats 400 s of the 1200 s run budget
        return await real(requests)

    monkeypatch.setattr(claude_batch, "submit", slow_submit)
    asyncio.run(pipeline.run_ambient_pipeline(1))
    # Rounds start at 0, 400 and 800 s; after that less than 120 s is left.
    total = len(_entry(world, "summaries")["batches"]) + len(_entry(world, "entities")["batches"])
    assert total == 3


def test_poll_skips_top_up_when_its_budget_is_nearly_spent(env, monkeypatch):
    world, api, polls, _, _ = env(max_inflight=1)
    asyncio.run(pipeline._maybe_submit_batch(1, "summaries"))
    api.finish("msgbatch_1")
    clock = {"t": 0.0}
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock["t"])
    # Apply finished inside its budget, but the request has < 120 s left.
    monkeypatch.setattr(pipeline, "BATCH_APPLY_BUDGET_S", pipeline.POLL_BUDGET_S)
    real = pipeline._apply_summary_results

    async def slow_apply(production_id, results, deadline):
        out = await real(production_id, results, deadline)
        clock["t"] = pipeline.POLL_BUDGET_S - pipeline.BATCH_SUBMIT_MIN_REMAINING_S + 1
        return out

    monkeypatch.setattr(pipeline, "_apply_summary_results", slow_apply)
    polls.clear()
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "summaries"))
    assert list(api.submitted) == ["msgbatch_1"]  # no top-up this time
    assert polls == [(1, "summaries", 300)]


def test_entities_apply_one_batch_at_a_time(env):
    world, api, _, _, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "entities"))
    entry = world.prod.ai_pipeline_status["entities"]
    entry["batches"][0]["state"] = "applying"
    entry["batches"][0]["applying_at"] = _now()
    entry["status"] = "applying"
    api.finish("msgbatch_1", "msgbatch_2")
    _clear_poll_schedule(world)
    asyncio.run(pipeline.poll_batch_stage(1, "entities"))
    states = [b["state"] for b in _entry(world, "entities")["batches"]]
    assert states == ["applying", "processing", "processing"]


def test_concurrent_entity_polls_never_apply_two_batches_at_once(env, monkeypatch):
    world, api, _, pipelines, _ = env()
    asyncio.run(pipeline._maybe_submit_batch(1, "entities"))
    for b in _entry(world, "entities")["batches"]:
        api.finish(b["id"])
    _clear_poll_schedule(world)
    live, peak = {"n": 0}, {"n": 0}
    real = pipeline._apply_entity_results

    async def tracking(production_id, results, deadline):
        live["n"] += 1
        peak["n"] = max(peak["n"], live["n"])
        await asyncio.sleep(0)
        try:
            return await real(production_id, results, deadline)
        finally:
            live["n"] -= 1

    monkeypatch.setattr(pipeline, "_apply_entity_results", tracking)

    async def three_polls():
        await asyncio.gather(*(pipeline.poll_batch_stage(1, "entities") for _ in range(3)))

    asyncio.run(three_polls())
    assert peak["n"] == 1
    # Whatever the concurrent polls skipped, the chain finishes.
    for _ in range(5):
        if _entry(world, "entities") == "done":
            break
        _clear_poll_schedule(world)
        asyncio.run(pipeline.poll_batch_stage(1, "entities"))
    assert _entry(world, "entities") == "done" and pipelines == [1]
