import asyncio
from types import SimpleNamespace

import app.routers.ingest as ri
import app.services.ingest_native as native_mod
import app.services.tasks as task_service
from app.services import large_ingest
from app.services.large_ingest import is_large_container, large_source_key, run_survey
from tests.fakes import FakeUser
from tests.test_source_designation import FakeBackgroundTasks, FakeProduction, UuidFakeSession

SMALL = {"storage_path": "productions/1/raw/small.eml", "relative_path": "small.eml",
         "filename": "small.eml", "size": 0}
BIG = {"storage_path": "productions/1/raw/big.pst", "relative_path": "big.pst",
       "filename": "big.pst", "size": 11_200_000_000}


def test_is_large_container_threshold():
    assert is_large_container({"filename": "a.pst", "size": 1_000_000_000})
    assert not is_large_container({"filename": "a.pst", "size": 999_999_999})
    assert not is_large_container({"filename": "a.zip", "size": 5_000_000_000})
    assert is_large_container({"filename": "B.OST", "size": 2_000_000_000})


def test_large_source_key():
    assert large_source_key("x/y.pst") == "native:x/y.pst"


def test_start_with_large_pst_enqueues_survey_and_sets_surveying(monkeypatch):
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [SMALL, BIG])
    batches, surveys = [], []
    monkeypatch.setattr(task_service, "enqueue_ingest_batch", lambda **kw: batches.append(kw))
    monkeypatch.setattr(task_service, "enqueue_survey", lambda j, p: surveys.append((j, p)))
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})

    asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native"},
        background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))

    job = db.added[0]
    assert job.status == "surveying"
    assert job.field_mapping["large_containers"] == [BIG["storage_path"]]
    assert job.total_files == 2
    assert len(batches) == 1  # normal batches still enqueue
    assert surveys == [(str(job.id), BIG["storage_path"])]


def test_start_without_large_container_stays_processing(monkeypatch):
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [SMALL])
    monkeypatch.setattr(task_service, "enqueue_ingest_batch", lambda **kw: None)
    surveys = []
    monkeypatch.setattr(task_service, "enqueue_survey", lambda j, p: surveys.append(p))
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})
    asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native"},
        background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    job = db.added[0]
    assert job.status == "processing" and "large_containers" not in job.field_mapping
    assert surveys == []


def test_native_batch_skips_large_containers(monkeypatch):
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [BIG])
    job = SimpleNamespace(id="j1", field_mapping={}, errors=[], source_format="native")
    db = UuidFakeSession(get_objects={
        ("IngestJob", "j1"): job,
        ("Production", 1): SimpleNamespace(id=1, name="Matter"),
    })
    downloaded = []
    monkeypatch.setattr(native_mod, "get_download_bytes", lambda p: downloaded.append(p) or b"")
    skipped = []
    import app.services.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "_incr_skipped", lambda *a, **k: skipped.append(a))
    monkeypatch.setattr(ingest_mod, "_mark_source_done", lambda *a, **k: skipped.append(a))

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(ingest_mod, "_finalize_job_if_done", noop)
    asyncio.run(native_mod.ingest_native_batch(db, "j1", 1, 0, 10))
    assert downloaded == [] and skipped == []


def test_survey_sets_awaiting_selection_when_all_surveyed(monkeypatch):
    job = SimpleNamespace(
        id="j1", status="surveying", survey={},
        field_mapping={"large_containers": ["a.pst", "b.pst"]},
    )
    from tests.fakes import FakeResult

    # The per-container attempt counter (C1) answers attempt 1.
    db = UuidFakeSession(get_objects={("IngestJob", "j1"): job},
                         responders=[("survey_attempts", FakeResult(scalar=1))])
    monkeypatch.setattr(large_ingest, "_survey_container",
                        lambda p: {"storage_path": p, "folders": []})

    asyncio.run(run_survey(db, "j1", "a.pst"))
    assert job.status == "surveying" and set(job.survey) == {"a.pst"}
    asyncio.run(run_survey(db, "j1", "b.pst"))
    assert job.status == "awaiting_selection" and set(job.survey) == {"a.pst", "b.pst"}


def test_survey_locked_reread_does_not_use_stale_job(monkeypatch):
    """The locked re-read must refresh from the DB: a concurrent survey's write
    (present only on the fresh row) must survive."""
    stale = SimpleNamespace(
        id="j1", status="surveying", survey={},
        field_mapping={"large_containers": ["a.pst", "b.pst"]},
    )
    fresh = SimpleNamespace(
        id="j1", status="surveying",
        survey={"a.pst": {"storage_path": "a.pst", "folders": []}},
        field_mapping={"large_containers": ["a.pst", "b.pst"]},
    )

    class SeqSession(UuidFakeSession):
        def __init__(self):
            from tests.fakes import FakeResult

            super().__init__(responders=[("survey_attempts", FakeResult(scalar=1))])
            self.calls = []

        async def get(self, model, key, **kw):
            self.calls.append(kw)
            return stale if len(self.calls) == 1 else fresh

    db = SeqSession()
    monkeypatch.setattr(large_ingest, "_survey_container",
                        lambda p: {"storage_path": p, "folders": []})
    asyncio.run(run_survey(db, "j1", "b.pst"))
    assert db.calls[1].get("with_for_update") and db.calls[1].get("populate_existing")
    assert set(fresh.survey) == {"a.pst", "b.pst"}
    assert fresh.status == "awaiting_selection"


def test_inline_mode_reports_and_skips_large_containers(monkeypatch):
    monkeypatch.setattr(task_service, "is_configured", lambda: False)
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [SMALL, BIG])
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})
    bg = FakeBackgroundTasks()
    asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native", "total_files": 2},
        background_tasks=bg, db=db, user=FakeUser()))
    job = db.added[0]
    assert job.errors == ["big.pst: large mailbox loading requires Cloud Tasks"]
    skipped = [p for p in db.executed_params if p and p.get("key") == "native:" + BIG["storage_path"]]
    assert len(skipped) == 1 and skipped[0]["jid"] == str(job.id)
    assert len(bg.tasks) == 1


# --- C1: survey attempt bound and failure state ------------------------------

import json  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.services.large_ingest import MAX_SURVEY_ATTEMPTS  # noqa: E402


class SurveySession(UuidFakeSession):
    """Simulates the survey statements against one job namespace."""

    def __init__(self, job):
        super().__init__(get_objects={("IngestJob", job.id): job})
        self.job = job
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, stmt, params=None):
        from tests.fakes import FakeResult

        sql = str(stmt)
        self.executed.append(sql)
        self.executed_params.append(params)
        job = self.job
        if "survey_attempts" in sql:
            if job.status != "surveying":
                return FakeResult(scalar=None)
            fm = dict(job.field_mapping)
            counts = dict(fm.get("survey_attempts") or {})
            counts[params["path"]] = counts.get(params["path"], 0) + 1
            fm["survey_attempts"] = counts
            job.field_mapping = fm
            return FakeResult(scalar=counts[params["path"]])
        if "errors = errors ||" in sql:
            job.errors = list(job.errors) + json.loads(params["new"])
            return FakeResult(rowcount=1)
        if "skipped_keys = skipped_keys ||" in sql:
            if params["key"] not in job.skipped_keys:
                job.skipped_keys = list(job.skipped_keys) + [params["key"]]
                job.skipped_files += 1
            return FakeResult(rowcount=1)
        return FakeResult()


def _survey_job(paths, total_files=None, status="surveying"):
    return SimpleNamespace(
        id="j1", status=status, survey={}, errors=[], skipped_keys=[], skipped_files=0,
        total_files=len(paths) if total_files is None else total_files, completed_at=None,
        field_mapping={"large_containers": list(paths)},
    )


def _failing_survey(monkeypatch, bad=("a.pst",)):
    calls = []

    def fake(p):
        calls.append(p)
        if p in bad:
            raise OSError("bad block at offset 4096")
        return {"storage_path": p, "folders": []}

    monkeypatch.setattr(large_ingest, "_survey_container", fake)
    return calls


def _handler(db, path):
    return asyncio.run(ri.survey_container_handler(body={"job_id": "j1", "storage_path": path}, db=db))


def test_max_survey_attempts():
    assert MAX_SURVEY_ATTEMPTS == 3


def test_survey_failing_three_times_fails_the_job_and_stops_retries(monkeypatch):
    job = _survey_job(["productions/1/raw/a.pst"])
    db = SurveySession(job)
    calls = _failing_survey(monkeypatch, bad=("productions/1/raw/a.pst",))
    for _ in range(MAX_SURVEY_ATTEMPTS - 1):
        with pytest.raises(HTTPException) as e:
            _handler(db, "productions/1/raw/a.pst")
        assert e.value.status_code == 500
        assert job.status == "surveying" and job.errors == []
    # Third failure: given up, answered 200 so Cloud Tasks stops.
    assert _handler(db, "productions/1/raw/a.pst") == {"ok": True}
    assert job.status == "failed" and job.completed_at is not None
    (msg,) = job.errors
    assert msg.startswith("a.pst: mailbox could not be read (survey failed 3 times): ")
    assert "bad block" in msg
    assert job.skipped_keys == [large_source_key("productions/1/raw/a.pst")]
    assert job.field_mapping["survey_failed"]["productions/1/raw/a.pst"]
    # A redelivery does no work.
    assert _handler(db, "productions/1/raw/a.pst") == {"ok": True}
    assert len(calls) == MAX_SURVEY_ATTEMPTS and len(job.errors) == 1


def test_survey_attempts_from_dead_workers_count(monkeypatch):
    """Attempts that died without raising (OOM/timeout) still count: the next
    claim past the cap gives up without walking the container again."""
    job = _survey_job(["a.pst"])
    job.field_mapping["survey_attempts"] = {"a.pst": MAX_SURVEY_ATTEMPTS}
    calls = _failing_survey(monkeypatch, bad=())
    asyncio.run(run_survey(SurveySession(job), "j1", "a.pst"))
    assert calls == []
    assert job.status == "failed"
    assert job.errors == ["a.pst: mailbox could not be read (survey failed 3 times): "
                          "the worker stopped before the survey finished"]


def test_mixed_job_continues_without_the_failed_container(monkeypatch):
    job = _survey_job(["a.pst", "b.pst"])
    db = SurveySession(job)
    _failing_survey(monkeypatch, bad=("a.pst",))
    asyncio.run(run_survey(db, "j1", "b.pst"))
    assert job.status == "surveying"
    for _ in range(MAX_SURVEY_ATTEMPTS - 1):
        with pytest.raises(OSError):
            asyncio.run(run_survey(db, "j1", "a.pst"))
    asyncio.run(run_survey(db, "j1", "a.pst"))
    # b.pst still goes to selection; a.pst is reported and skipped.
    assert job.status == "awaiting_selection"
    assert set(job.survey) == {"b.pst"}
    assert len(job.errors) == 1 and job.errors[0].startswith("a.pst: mailbox could not be read")
    assert job.skipped_keys == [large_source_key("a.pst")]


def test_failed_container_with_normal_files_goes_on_to_finalize(monkeypatch):
    job = _survey_job(["a.pst"], total_files=3)
    db = SurveySession(job)
    _failing_survey(monkeypatch, bad=("a.pst",))
    finalized = []

    async def fake_finalize(db, job_id):
        finalized.append(job_id)

    monkeypatch.setattr(large_ingest, "maybe_finalize", fake_finalize)
    job.field_mapping["survey_attempts"] = {"a.pst": MAX_SURVEY_ATTEMPTS - 1}
    asyncio.run(run_survey(db, "j1", "a.pst"))
    assert job.status == "processing" and finalized == ["j1"]

