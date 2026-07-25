"""Fake-session tests for the timeline-review trigger + status endpoints."""

import asyncio

import pytest
from fastapi import HTTPException

import app.routers.entities as er
import app.services.tasks as task_service
from app.models import AuditLog, Production
from tests.fakes import FakeResult, FakeSession, FakeUser


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args):
        self.tasks.append((fn, args))


def _patch(monkeypatch, accessible=(1,), role="manager", configured=False):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)
    import app.dependencies as deps
    async def fake_role(db, user, pid):
        return role
    monkeypatch.setattr(deps, "get_user_role_for_production", fake_role)
    # Deterministic dispatch mode: tests that care about Cloud Tasks flip
    # this explicitly via _patch(..., configured=True).
    monkeypatch.setattr(task_service, "is_configured", lambda: configured)


def test_trigger_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.trigger_timeline_review(
            production_id=1, background_tasks=FakeBackgroundTasks(),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


def test_trigger_denies_below_manager(monkeypatch):
    _patch(monkeypatch, role="member")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.trigger_timeline_review(
            production_id=1, background_tasks=FakeBackgroundTasks(),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_trigger_kicks_off_background_run(monkeypatch):
    _patch(monkeypatch, configured=False)
    bg = FakeBackgroundTasks()
    db = FakeSession()
    out = asyncio.run(er.trigger_timeline_review(
        production_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert out == {"status": "started"}
    from app.services.pipeline import run_timeline_review_stage
    assert bg.tasks == [(run_timeline_review_stage, (1,))]
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert audits and audits[0].action == "timeline_review_triggered"


def test_trigger_409_when_running(monkeypatch):
    _patch(monkeypatch)
    prod = Production(name="M")
    prod.id = 1
    prod.ai_pipeline_status = {"timeline_review": "running"}
    db = FakeSession(get_objects={("Production", 1): prod})
    bg = FakeBackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.trigger_timeline_review(
            production_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert exc.value.status_code == 409
    assert db.added == []
    assert bg.tasks == []


def test_trigger_409_when_queued(monkeypatch):
    _patch(monkeypatch)
    prod = Production(name="M")
    prod.id = 1
    prod.ai_pipeline_status = {"timeline_review": "queued"}
    db = FakeSession(get_objects={("Production", 1): prod})
    bg = FakeBackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.trigger_timeline_review(
            production_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert exc.value.status_code == 409
    assert db.added == []
    assert bg.tasks == []


def test_trigger_force_bypasses_409(monkeypatch):
    _patch(monkeypatch, configured=False)
    prod = Production(name="M")
    prod.id = 1
    prod.ai_pipeline_status = {"timeline_review": "running"}
    db = FakeSession(get_objects={("Production", 1): prod})
    bg = FakeBackgroundTasks()
    out = asyncio.run(er.trigger_timeline_review(
        production_id=1, background_tasks=bg, db=db, user=FakeUser(), force=True))
    assert out == {"status": "started"}
    from app.services.pipeline import run_timeline_review_stage
    assert bg.tasks == [(run_timeline_review_stage, (1,))]
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert audits and audits[0].action == "timeline_review_triggered"


def test_trigger_enqueues_via_cloud_tasks(monkeypatch):
    _patch(monkeypatch, configured=True)
    calls = []
    monkeypatch.setattr(task_service, "enqueue_pipeline", lambda pid, *a, **k: calls.append(pid))
    prod = Production(name="M")
    prod.id = 1
    db = FakeSession(get_objects={("Production", 1): prod})
    bg = FakeBackgroundTasks()
    out = asyncio.run(er.trigger_timeline_review(
        production_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert out == {"status": "enqueued"}
    assert calls == [1]
    assert bg.tasks == []
    assert prod.ai_pipeline_status["timeline_review"] == "queued"
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert audits and audits[0].action == "timeline_review_triggered"


def test_status_reports_stage_and_latest_summary(monkeypatch):
    _patch(monkeypatch)
    prod = Production(name="M")
    prod.id = 1
    prod.ai_pipeline_status = {"timeline_review": "failed",
                               "errors": {"timeline_review": "boom"}}
    row = AuditLog(user_id="u1", user_email="u1@thirulaw.com",
                   action="timeline_review_completed", resource_type="production",
                   resource_id="1", production_id=1,
                   details={"merged": 2, "deleted": 1, "edited": 0, "skipped": 3})
    db = FakeSession(get_objects={("Production", 1): prod},
                     responders=[("FROM audit_logs", FakeResult(items=[row]))])
    out = asyncio.run(er.get_timeline_review_status(
        production_id=1, db=db, user=FakeUser()))
    assert out["state"] == "failed" and out["error"] == "boom"
    assert out["summary"]["merged"] == 2


def test_status_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_timeline_review_status(
            production_id=1, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404
