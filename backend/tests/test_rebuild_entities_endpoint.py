"""Fake-session tests for the destructive extract-entities?rebuild=true path.

This is the ONLY destructive re-extraction endpoint (the old /reset-entities
in entities.py was consolidated into it). The critical regression guarded here
is C1: stages_to_run() skips any stage marked "done", so a rebuild that wipes
the ontology but leaves entities: "done" in Production.ai_pipeline_status is a
destructive no-op — the enqueued pipeline exits without re-extracting and the
matter is stranded permanently empty."""

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

import app.routers.ingest as ingest_router
import app.services.pipeline as pipeline
import app.services.tasks as task_service
from tests.fakes import FakeSession, FakeUser


class FakeProduction:
    def __init__(self, status=None):
        self.id = 1
        self.ai_pipeline_status = status


ALL_DONE = {"clustering": "done", "summaries": "done", "entities": "done",
            "brief": "done", "errors": {}}


def _patch(monkeypatch, accessible=(1,), role="manager", configured=True):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        return None

    # The endpoint imports these from app.dependencies at call time.
    monkeypatch.setattr("app.dependencies.get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr("app.dependencies.get_user_role_for_production", fake_role)
    monkeypatch.setattr(ingest_router, "log_action", fake_log)
    monkeypatch.setattr(task_service, "is_configured", lambda: configured)


def _call(db, production_id=1, rebuild=True, bt=None):
    return asyncio.run(ingest_router.trigger_entity_extraction(
        production_id=production_id, background_tasks=bt or BackgroundTasks(),
        rebuild=rebuild, db=db, user=FakeUser()))


def test_rebuild_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        _call(FakeSession())
    assert exc.value.status_code == 404


def test_rebuild_requires_manager(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    with pytest.raises(HTTPException) as exc:
        _call(FakeSession())
    assert exc.value.status_code == 403


def test_rebuild_deletes_clears_and_enqueues(monkeypatch):
    _patch(monkeypatch)
    calls = []
    monkeypatch.setattr(task_service, "enqueue_pipeline", lambda pid, *a, **k: calls.append(pid))
    db = FakeSession(get_objects={("Production", 1): FakeProduction(dict(ALL_DONE))})
    out = _call(db)

    assert out == {"status": "enqueued"}
    assert calls == [1]
    joined = "\n".join(db.executed)
    # full ontology deleted — participants and events explicitly (they cascade
    # from productions, not entities), then the entity referents + entities
    assert "DELETE FROM event_participants" in joined
    assert "DELETE FROM ontology_events" in joined
    assert "DELETE FROM entity_mentions" in joined
    assert "DELETE FROM entity_relationships" in joined
    assert "DELETE FROM entity_merge_suggestions" in joined
    assert "DELETE FROM entities" in joined
    # per-document extraction watermark cleared
    assert "UPDATE documents SET entities_extracted_at" in joined


def test_rebuild_clears_stage_state_so_reextraction_actually_runs(monkeypatch):
    """C1 regression guard: after the destructive call, the pipeline stage
    state must actually permit re-running. If someone reintroduces the skip
    (leaves entities: "done" in place without forcing), stages_to_run() drops
    the entities stage and this fails. If the implementation ever switches to
    passing force=True through the enqueue instead of clearing stage keys,
    update this test to assert that flag."""
    _patch(monkeypatch)
    monkeypatch.setattr(task_service, "enqueue_pipeline", lambda *a, **k: None)
    prod = FakeProduction(dict(ALL_DONE))
    db = FakeSession(get_objects={("Production", 1): prod})
    _call(db)

    pending = pipeline.stages_to_run(prod.ai_pipeline_status, force=False)
    assert "entities" in pending
    assert "brief" in pending  # the brief derives from entities: must regenerate
    # stages whose artifacts the rebuild leaves intact must NOT re-run
    assert "clustering" not in pending
    assert "summaries" not in pending


def test_nonrebuild_trigger_is_not_destructive(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(task_service, "enqueue_pipeline", lambda *a, **k: None)
    prod = FakeProduction(dict(ALL_DONE))
    db = FakeSession(get_objects={("Production", 1): prod})
    out = _call(db, rebuild=False)
    assert out == {"status": "enqueued"}
    assert db.executed == []  # no deletes, no watermark clear
    assert prod.ai_pipeline_status == ALL_DONE  # stage state untouched


def test_rebuild_enqueues_after_commit(monkeypatch):
    """The cleared entities_extracted_at watermarks and stage state must be
    durably committed BEFORE the re-extraction worker is enqueued, or the
    worker can read stale state and skip documents. Assert commit precedes
    enqueue via a shared call-order list (FakeSession.commit is otherwise a
    no-op)."""
    _patch(monkeypatch)
    order = []

    class OrderedSession(FakeSession):
        async def commit(self):
            order.append("commit")

    monkeypatch.setattr(task_service, "enqueue_pipeline",
                        lambda pid, *a, **k: order.append("enqueue"))
    db = OrderedSession()
    out = _call(db)
    assert out == {"status": "enqueued"}
    assert order == ["commit", "enqueue"]


def test_rebuild_falls_back_to_background_when_unconfigured(monkeypatch):
    _patch(monkeypatch, configured=False)
    monkeypatch.setattr(pipeline, "run_ambient_pipeline", lambda *a, **k: None)
    bt = BackgroundTasks()
    out = _call(FakeSession(), bt=bt)
    assert out == {"status": "started"}
    assert len(bt.tasks) == 1
