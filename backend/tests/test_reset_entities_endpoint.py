"""Fake-session tests for the destructive reset-entities endpoint."""

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

import app.routers.entities as er
import app.services.tasks as task_service
from tests.fakes import FakeSession, FakeUser


def _patch(monkeypatch, accessible=(1,), role="manager", configured=True):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(er, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(er, "log_action", fake_log)
    monkeypatch.setattr(task_service, "is_configured", lambda: configured)


def test_reset_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.reset_production_entities(
            production_id=1, background_tasks=BackgroundTasks(), db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


def test_reset_requires_manager(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.reset_production_entities(
            production_id=1, background_tasks=BackgroundTasks(), db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_reset_deletes_clears_and_enqueues(monkeypatch):
    _patch(monkeypatch, configured=True)
    calls = []
    monkeypatch.setattr(task_service, "enqueue_pipeline", lambda pid, *a, **k: calls.append(pid))
    db = FakeSession()
    out = asyncio.run(er.reset_production_entities(
        production_id=1, background_tasks=BackgroundTasks(), db=db, user=FakeUser()))

    assert out == {"reset": True}
    assert calls == [1]
    joined = "\n".join(db.executed)
    # entities deleted (cascade removes mentions/participants/relationships/suggestions)
    assert "DELETE FROM entities" in joined
    # events (and their participants) explicitly cleared
    assert "DELETE FROM ontology_events" in joined
    assert "DELETE FROM event_participants" in joined
    # per-document extraction watermark cleared
    assert "UPDATE documents SET entities_extracted_at" in joined


def test_reset_enqueues_after_commit(monkeypatch):
    """The cleared entities_extracted_at watermark must be durably committed
    BEFORE the re-extraction worker is enqueued, or the worker can read stale
    state and skip documents. Assert commit precedes enqueue via a shared
    call-order list (FakeSession.commit is otherwise a no-op)."""
    _patch(monkeypatch, configured=True)
    order = []

    class OrderedSession(FakeSession):
        async def commit(self):
            order.append("commit")

    monkeypatch.setattr(task_service, "enqueue_pipeline",
                        lambda pid, *a, **k: order.append("enqueue"))
    db = OrderedSession()
    out = asyncio.run(er.reset_production_entities(
        production_id=1, background_tasks=BackgroundTasks(), db=db, user=FakeUser()))
    assert out == {"reset": True}
    assert order == ["commit", "enqueue"]


def test_reset_falls_back_to_background_when_unconfigured(monkeypatch):
    _patch(monkeypatch, configured=False)
    import app.services.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_ambient_pipeline", lambda *a, **k: None)
    bt = BackgroundTasks()
    out = asyncio.run(er.reset_production_entities(
        production_id=1, background_tasks=bt, db=FakeSession(), user=FakeUser()))
    assert out == {"reset": True}
    assert len(bt.tasks) == 1
