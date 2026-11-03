"""Sticky AI hold: large normal loads hold the ambient pipeline; a held
production stays held until someone explicitly runs analysis; the estimate
endpoint prices the pending per-document AI work. Fake sessions only."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

import app.routers.ingest as ingest_router
import app.routers.productions as productions_router
import app.services.ingest as ingest_service
import app.services.tasks as task_service
from app.services.ingest import AI_HOLD_DOC_THRESHOLD, should_hold


def test_threshold_value():
    assert AI_HOLD_DOC_THRESHOLD == 5_000


def test_should_hold_rules():
    p = SimpleNamespace(ai_hold=False)
    assert should_hold(p, 5_001, False) and not should_hold(p, 5_000, False)
    assert should_hold(p, 10, True)
    assert should_hold(SimpleNamespace(ai_hold=True), 1, False)


# --- finalize ---------------------------------------------------------------

class FinalizeSession:
    def __init__(self, prod):
        self.prod = prod
        self.commits = 0

    async def refresh(self, obj):
        pass

    async def commit(self):
        self.commits += 1

    async def get(self, model, key, **_kw):
        if model.__name__ == "Production" and key == self.prod.id:
            return self.prod
        return None


def _job(docs: int):
    return SimpleNamespace(
        id="job-1", field_mapping={}, status="processing", source_format="relativity",
        processed_files=docs, skipped_files=0, total_files=docs, errors=[],
        completed_at=None,
    )


def _patch_finalize(monkeypatch):
    calls = {"enqueue": [], "title_limits": []}

    async def fake_titles(db, production_id, errors, *, ai_limit=None, ai_timeout_s=None):
        calls["title_limits"].append(ai_limit)

    async def fake_embed(db, production_id):
        pass

    async def fake_threads(db, production_id):
        pass

    monkeypatch.setattr(ingest_service, "generate_missing_titles", fake_titles)
    monkeypatch.setattr("app.services.embeddings.embed_production_documents", fake_embed)
    monkeypatch.setattr("app.services.email_threading.derive_threads", fake_threads)
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    monkeypatch.setattr(task_service, "enqueue_pipeline",
                        lambda production_id, *a, **k: calls["enqueue"].append(production_id))
    return calls


def test_small_load_into_held_production_does_not_enqueue(monkeypatch):
    calls = _patch_finalize(monkeypatch)
    prod = SimpleNamespace(id=1, ai_hold=True)
    job = _job(3)
    asyncio.run(ingest_service._finalize_job_if_done(FinalizeSession(prod), job, 1, []))
    assert job.status == "complete"
    assert calls["enqueue"] == []
    assert prod.ai_hold is True


def test_large_normal_load_sets_hold(monkeypatch):
    calls = _patch_finalize(monkeypatch)
    prod = SimpleNamespace(id=1, ai_hold=False)
    db = FinalizeSession(prod)
    job = _job(6_000)
    asyncio.run(ingest_service._finalize_job_if_done(db, job, 1, []))
    assert job.status == "complete"
    assert prod.ai_hold is True
    assert calls["enqueue"] == []
    # Held loads don't AI-title every document inline (bounded like large loads).
    assert calls["title_limits"] and calls["title_limits"][0] is not None


def test_small_normal_load_still_enqueues(monkeypatch):
    calls = _patch_finalize(monkeypatch)
    prod = SimpleNamespace(id=1, ai_hold=False)
    asyncio.run(ingest_service._finalize_job_if_done(FinalizeSession(prod), _job(3), 1, []))
    assert calls["enqueue"] == [1]
    assert prod.ai_hold is False
    assert calls["title_limits"] == [None]


# --- explicit run endpoints clear the hold --------------------------------------

class EndpointSession:
    def __init__(self, prod):
        self.prod = prod
        self.commits = 0
        self.hold_at_commit: list = []

    async def get(self, model, key, **_kw):
        if model.__name__ == "Production" and key == self.prod.id:
            return self.prod
        return None

    async def commit(self):
        self.commits += 1
        self.hold_at_commit.append(self.prod.ai_hold)


class FakeUser:
    id = "u1"
    email = "u1@example.com"
    display_name = "u1"


def _patch_access(monkeypatch, module_prefix):
    async def fake_role(db, user, production_id):
        return "manager"

    async def fake_accessible(db, user):
        return [1]

    async def fake_log(*a, **k):
        pass

    monkeypatch.setattr(f"{module_prefix}.get_user_role_for_production", fake_role)
    monkeypatch.setattr(f"{module_prefix}.get_accessible_production_ids", fake_accessible)
    return fake_log


def test_run_endpoint_clears_hold_and_enqueues(monkeypatch):
    fake_log = _patch_access(monkeypatch, "app.routers.productions")
    monkeypatch.setattr(productions_router, "log_action", fake_log)
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    enq = []

    def fake_enqueue(production_id, force=False):
        # The worker must see the cleared hold: it was committed first.
        assert db.hold_at_commit and db.hold_at_commit[-1] is False
        enq.append(production_id)

    monkeypatch.setattr(task_service, "enqueue_pipeline", fake_enqueue)
    prod = SimpleNamespace(id=1, ai_hold=True, ai_pipeline_status=None)
    db = EndpointSession(prod)
    out = asyncio.run(productions_router.run_pipeline(
        production_id=1, background_tasks=BackgroundTasks(), body={}, db=db, user=FakeUser()))
    assert out == {"started": True}
    assert prod.ai_hold is False
    assert enq == [1]


def test_extract_entities_clears_hold_before_enqueue(monkeypatch):
    fake_log = _patch_access(monkeypatch, "app.dependencies")
    monkeypatch.setattr(ingest_router, "log_action", fake_log)
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    enq = []

    def fake_enqueue(production_id):
        assert db.hold_at_commit and db.hold_at_commit[-1] is False
        enq.append(production_id)

    monkeypatch.setattr(task_service, "enqueue_pipeline", fake_enqueue)
    prod = SimpleNamespace(id=1, ai_hold=True, ai_pipeline_status=None)
    db = EndpointSession(prod)
    out = asyncio.run(ingest_router.trigger_entity_extraction(
        production_id=1, background_tasks=BackgroundTasks(), db=db, user=FakeUser()))
    assert out == {"status": "enqueued"}
    assert prod.ai_hold is False
    assert enq == [1]


# --- estimate -----------------------------------------------------------------

class EstimateResult:
    def __init__(self, row):
        self._row = row

    def one(self):
        return self._row


class EstimateSession(EndpointSession):
    def __init__(self, prod, row):
        super().__init__(prod)
        self.row = row

    async def execute(self, stmt, params=None):
        return EstimateResult(self.row)


def _patch_role(monkeypatch, role):
    async def fake_role(db, user, production_id):
        return role

    monkeypatch.setattr("app.dependencies.get_user_role_for_production", fake_role)


@pytest.mark.parametrize("role,can_run", [
    ("admin", True), ("manager", True), ("reviewer", False), ("readonly", False)])
def test_ai_estimate_can_run_follows_role(monkeypatch, role, can_run):
    _patch_access(monkeypatch, "app.dependencies")
    _patch_role(monkeypatch, role)
    out = asyncio.run(ingest_router.ai_estimate(
        production_id=1, db=EstimateSession(SimpleNamespace(id=1, ai_hold=True), (0, 0, 0, 0, 0)),
        user=FakeUser()))
    assert out["can_run"] is can_run


def test_ai_estimate_shape_and_pricing(monkeypatch):
    _patch_access(monkeypatch, "app.dependencies")
    _patch_role(monkeypatch, "admin")
    prod = SimpleNamespace(id=1, ai_hold=True)
    # 10 pending docs; 10 need summaries (120,000 summary chars), 10 need
    # entities (400,000 chars).
    row = (10, 10, 120_000, 10, 400_000)
    out = asyncio.run(ingest_router.ai_estimate(
        production_id=1, db=EstimateSession(prod, row), user=FakeUser()))
    assert set(out) == {"held", "documents_pending", "usd_estimate", "can_run"}
    assert out["held"] is True
    assert out["documents_pending"] == 10
    from app.services.ai import ENTITY_PROMPT_OVERHEAD_TOKENS, SUMMARY_PROMPT_OVERHEAD_TOKENS

    input_tokens = ((120_000 + 400_000) / 4
                    + 10 * SUMMARY_PROMPT_OVERHEAD_TOKENS + 10 * ENTITY_PROMPT_OVERHEAD_TOKENS)
    output_tokens = 10 * 300 + 10 * 1_000
    expected = input_tokens * 0.05 / 1e6 + output_tokens * 0.25 / 1e6
    assert out["usd_estimate"] == pytest.approx(expected, abs=1e-4)


def test_ai_estimate_404_outside_scope(monkeypatch):
    _patch_access(monkeypatch, "app.dependencies")
    prod = SimpleNamespace(id=2, ai_hold=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ingest_router.ai_estimate(
            production_id=2, db=EstimateSession(prod, (0, 0, 0, 0, 0)), user=FakeUser()))
    assert exc.value.status_code == 404


# --- I2: cheap estimate polling --------------------------------------------


@pytest.fixture(autouse=True)
def _clear_estimate_cache():
    ingest_router._AI_ESTIMATE_CACHE.clear()
    yield
    ingest_router._AI_ESTIMATE_CACHE.clear()


class CountingEstimateSession(EstimateSession):
    def __init__(self, prod, row):
        super().__init__(prod, row)
        self.queries = 0

    async def execute(self, stmt, params=None):
        self.queries += 1
        return await super().execute(stmt, params)


def test_ai_estimate_skips_the_scan_when_not_held(monkeypatch):
    _patch_access(monkeypatch, "app.dependencies")
    _patch_role(monkeypatch, "manager")
    db = CountingEstimateSession(SimpleNamespace(id=1, ai_hold=False), (5, 5, 50, 5, 50))
    out = asyncio.run(ingest_router.ai_estimate(production_id=1, db=db, user=FakeUser()))
    assert out == {"held": False, "documents_pending": 0, "usd_estimate": 0, "can_run": True}
    assert db.queries == 0


def test_ai_estimate_is_cached_per_production_while_held(monkeypatch):
    _patch_access(monkeypatch, "app.dependencies")
    _patch_role(monkeypatch, "admin")
    clock = {"t": 1000.0}
    monkeypatch.setattr(ingest_router, "_monotonic", lambda: clock["t"])
    prod = SimpleNamespace(id=1, ai_hold=True)
    db = CountingEstimateSession(prod, (10, 10, 120_000, 10, 400_000))
    first = asyncio.run(ingest_router.ai_estimate(production_id=1, db=db, user=FakeUser()))
    clock["t"] += ingest_router.AI_ESTIMATE_CACHE_S - 1
    again = asyncio.run(ingest_router.ai_estimate(production_id=1, db=db, user=FakeUser()))
    assert db.queries == 1 and again == first
    clock["t"] += 2
    asyncio.run(ingest_router.ai_estimate(production_id=1, db=db, user=FakeUser()))
    assert db.queries == 2


def test_ai_estimate_cache_prunes_old_entries(monkeypatch):
    _patch_access(monkeypatch, "app.dependencies")
    _patch_role(monkeypatch, "admin")
    clock = {"t": 0.0}
    monkeypatch.setattr(ingest_router, "_monotonic", lambda: clock["t"])
    ingest_router._AI_ESTIMATE_CACHE[99] = (-4000.0, {"documents_pending": 1, "usd_estimate": 1})
    db = CountingEstimateSession(SimpleNamespace(id=1, ai_hold=True), (0, 0, 0, 0, 0))
    asyncio.run(ingest_router.ai_estimate(production_id=1, db=db, user=FakeUser()))
    assert 99 not in ingest_router._AI_ESTIMATE_CACHE


def test_prompt_overhead_constants_track_the_prompts():
    from app.services.ai import (ENTITY_PROMPT_OVERHEAD_TOKENS, SUMMARY_PROMPT,
                                 SUMMARY_PROMPT_OVERHEAD_TOKENS)
    from app.services.entity_extraction import EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt

    summary = len(SUMMARY_PROMPT) / 4
    entity = (len(EXTRACTION_SYSTEM_PROMPT) + len(build_extraction_prompt(""))) / 4
    assert summary * 0.8 <= SUMMARY_PROMPT_OVERHEAD_TOKENS <= summary * 1.25
    assert entity * 0.8 <= ENTITY_PROMPT_OVERHEAD_TOKENS <= entity * 1.25


# --- no hold while a requested analysis is running ------------------------------

INFO = "AI analysis already running; run analysis again after it finishes to include these new documents"


def _now(**delta):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _waiting_status():
    return {"clustering": "done", "updated_at": _now(),
            "summaries": {"status": "waiting", "submitted_at": _now(), "exhausted": True,
                          "batches": [{"id": "b1", "state": "processing",
                                       "submitted_at": _now(), "doc_count": 600}]}}


def test_normal_large_load_into_a_running_analysis_does_not_hold(monkeypatch):
    calls = _patch_finalize(monkeypatch)
    prod = SimpleNamespace(id=1, ai_hold=False, ai_pipeline_status=_waiting_status())
    job = _job(6_000)
    asyncio.run(ingest_service._finalize_job_if_done(FinalizeSession(prod), job, 1, []))
    assert job.status == "complete"
    assert prod.ai_hold is False
    assert INFO in job.errors
    # The running analysis owns the matter: no second pipeline run is started.
    assert calls["enqueue"] == []
    # Still a large load: bounded inline AI titling.
    assert calls["title_limits"][0] is not None


def test_normal_large_load_into_an_idle_production_still_holds(monkeypatch):
    calls = _patch_finalize(monkeypatch)
    prod = SimpleNamespace(id=1, ai_hold=False, ai_pipeline_status={"brief": "done"})
    job = _job(6_000)
    asyncio.run(ingest_service._finalize_job_if_done(FinalizeSession(prod), job, 1, []))
    assert prod.ai_hold is True
    assert INFO not in job.errors
    assert calls["enqueue"] == []
