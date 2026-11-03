"""Task 11: status endpoint exposes message progress, per-container survey, and self-heals."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

import app.routers.ingest as ri
from app.services import large_ingest
from app.services.large_ingest_estimate import estimate_load, estimate_model
from app.services.pst_reader import ContainerSurvey, FolderSurvey
from tests.fakes import FakeResult, FakeSession, FakeUser

JID = "11111111-1111-4111-8111-111111111111"
BIG = "productions/1/raw/loads/L1/acme.pst"
TS = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _survey():
    return ContainerSurvey(BIG, [
        FolderSurvey(0, "", False, {}, {}, []),
        FolderSurvey(1, "Top/Inbox", False, {"email": 5}, {"email": 5000}, [["email", 5]]),
        FolderSurvey(2, "Top/Contacts", False, {"contact": 3}, {"contact": 300}, [["contact", 3]]),
        FolderSurvey(3, "Top/Recoverable Items", True, {"email": 2}, {"email": 4000}, [["email", 2]]),
    ])


def _job(**kw):
    base = dict(
        id=JID, user_id="u1", production_id=1, status="awaiting_selection", source_format="native",
        total_files=1, processed_files=0, skipped_files=0, skipped_keys=[], done_keys=[],
        errors=[], created_at=TS, completed_at=None,
        survey={BIG: _survey().to_json()}, selection={},
        items_total=0, items_done=0, items_failed=0,
        field_mapping={"large_containers": [BIG]},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _db(job, responders=None):
    return FakeSession(get_objects={
        ("IngestJob", JID): job,
        ("Production", 1): SimpleNamespace(id=1, name="Acme Corp"),
    }, responders=responders or [])


def _status(job, db=None, bg=None):
    return asyncio.run(ri.get_ingest_status(
        job_id=JID, background_tasks=bg or BackgroundTasks(),
        db=db or _db(job), user=FakeUser("u1")))


@pytest.fixture(autouse=True)
def _clear_throttle():
    ri._SELF_HEAL_AT.clear()
    ri._QUIET_CHECKED_AT.clear()


def test_status_exposes_items_and_container_survey():
    out = _status(_job())
    assert out.items_total == 0 and out.items_done == 0
    c = out.containers[0]
    assert c.storage_path == BIG and c.name == "acme.pst" and c.selected is False
    assert c.size_bytes == 9300
    types = {t.type: t.model_dump() for t in c.types}
    assert types["email"] == {"type": "email", "count": 5, "bytes": 5000}
    assert types["contact"]["count"] == 3
    # Recoverable is reported separately, per type, so the client can toggle it.
    assert c.recoverable.count == 2 and c.recoverable.bytes == 4000
    assert [t.model_dump() for t in c.recoverable.types] == [{"type": "email", "count": 2, "bytes": 4000}]
    # Default selection: email + calendar + recoverable.
    assert c.estimate == estimate_load(_survey(), {"email", "calendar"}, True)
    assert out.estimate_model == estimate_model()
    assert out.estimate_model["budget_usd"] == 100.0


def test_status_after_selection_reports_progress_and_chosen_estimate():
    chosen = estimate_load(_survey(), {"email"}, False)
    job = _job(status="processing", items_total=7, items_done=3, items_failed=1,
               selection={BIG: {"types": ["email"], "include_recoverable": False, "estimate": chosen}})
    out = _status(job)
    assert (out.items_total, out.items_done, out.items_failed) == (7, 3, 1)
    assert out.containers[0].selected is True
    assert out.containers[0].estimate == chosen


def test_small_job_has_no_containers():
    out = _status(_job(survey={}, status="complete", field_mapping={}))
    assert out.containers == []


def test_awaiting_endpoint_lists_only_owned_awaiting_jobs():
    rows = [SimpleNamespace(id=JID, survey={BIG: _survey().to_json()}, field_mapping={"large_containers": [BIG]},
                            selection={})]
    db = FakeSession(responders=[("ingest_jobs", FakeResult(items=rows))])
    out = asyncio.run(ri.ingest_awaiting(production_id=1, db=db, user=FakeUser("u1")))
    assert out == [{"job_id": JID, "name": "acme.pst", "size_bytes": 9300}]


# --- self-heal ---------------------------------------------------------------


def _heal_env(monkeypatch):
    calls = []

    async def fake_finalize(db, job_id):
        calls.append(job_id)

    monkeypatch.setattr(large_ingest, "maybe_finalize", fake_finalize)
    import app.database as dbmod

    class Ctx:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dbmod, "async_session_factory", lambda: Ctx())
    return calls


def _run_bg(bg):
    async def go():
        for t in bg.tasks:
            await t()
    asyncio.run(go())


STALE = [("ingest_chunks", FakeResult(scalar=True))]
FRESH = [("ingest_chunks", FakeResult(scalar=False))]


def test_stale_processing_job_triggers_finalize_after_response(monkeypatch):
    calls = _heal_env(monkeypatch)
    job = _job(status="processing")
    bg = BackgroundTasks()
    _status(job, _db(job, STALE), bg)
    assert calls == []  # not inline
    _run_bg(bg)
    assert calls == [JID]


def test_recent_activity_does_not_trigger(monkeypatch):
    calls = _heal_env(monkeypatch)
    job = _job(status="finalizing")
    bg = BackgroundTasks()
    _status(job, _db(job, FRESH), bg)
    _run_bg(bg)
    assert calls == []


def test_non_running_statuses_never_heal(monkeypatch):
    calls = _heal_env(monkeypatch)
    for st in ("awaiting_selection", "complete", "failed", "surveying"):
        job = _job(status=st)
        bg = BackgroundTasks()
        _status(job, _db(job, STALE), bg)
        _run_bg(bg)
    assert calls == []


def test_heal_is_throttled_per_job(monkeypatch):
    calls = _heal_env(monkeypatch)
    job = _job(status="processing")
    for _ in range(3):
        bg = BackgroundTasks()
        _status(job, _db(job, STALE), bg)
        _run_bg(bg)
    assert calls == [JID]


def test_heal_errors_are_logged_not_surfaced(monkeypatch):
    _heal_env(monkeypatch)

    async def boom(db, job_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(large_ingest, "maybe_finalize", boom)
    job = _job(status="processing")
    bg = BackgroundTasks()
    _status(job, _db(job, STALE), bg)
    _run_bg(bg)  # must not raise


def test_quiet_check_runs_at_most_once_per_interval_per_job(monkeypatch):
    _heal_env(monkeypatch)
    job = _job(status="processing")
    db = _db(job, FRESH)
    for _ in range(5):
        _status(job, db, BackgroundTasks())
    quiet_queries = [q for q in db.executed if "ingest_chunks" in q]
    assert len(quiet_queries) == 1
    # After the interval it checks again.
    ri._QUIET_CHECKED_AT[str(JID)] -= ri.QUIET_CHECK_INTERVAL_S + 1
    _status(job, db, BackgroundTasks())
    assert len([q for q in db.executed if "ingest_chunks" in q]) == 2
