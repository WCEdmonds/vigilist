import asyncio
from types import SimpleNamespace

import pytest

import app.routers.ingest as ri
import app.services.ingest_native as native_mod
import app.services.tasks as task_service
from app.services import large_ingest, pst_explode
from app.services.large_ingest import is_large_container, large_source_key, run_survey
from tests.fakes import FakeUser
from tests.test_source_designation import FakeBackgroundTasks, FakeProduction, UuidFakeSession

SMALL = {"storage_path": "productions/1/raw/small.eml", "relative_path": "small.eml",
         "filename": "small.eml", "size": 0}
BIG = {"storage_path": "productions/1/raw/big.pst", "relative_path": "big.pst",
       "filename": "big.pst", "size": 11_200_000_000}


@pytest.fixture(autouse=True)
def deleted(monkeypatch):
    """No Storage: record exploded-prefix deletes."""
    out = []
    monkeypatch.setattr(pst_explode, "delete_exploded", lambda p: out.append(p) or 0)
    return out


def _work(p):
    return {"storage_path": p, "folders": []}


def test_is_large_container_threshold():
    assert is_large_container({"filename": "a.pst", "size": 1_000_000_000})
    assert not is_large_container({"filename": "a.pst", "size": 999_999_999})
    assert not is_large_container({"filename": "a.zip", "size": 5_000_000_000})
    assert is_large_container({"filename": "B.OST", "size": 2_000_000_000})


def test_large_source_key():
    assert large_source_key("x/y.pst") == "native:x/y.pst"


def test_start_with_large_pst_triggers_the_explode_job_and_sets_surveying(monkeypatch):
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [SMALL, BIG])
    batches, surveys = [], []
    monkeypatch.setattr(task_service, "enqueue_ingest_batch", lambda **kw: batches.append(kw))
    monkeypatch.setattr(pst_explode, "trigger_explode_job", lambda j, p: surveys.append((j, p)))
    monkeypatch.setattr(task_service, "_enqueue", lambda path, *a, **k: pytest.fail(f"task {path}"),
                        raising=True)
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
    assert not hasattr(task_service, "enqueue_survey")  # no survey Cloud Task any more
    assert not hasattr(ri, "survey_container_handler")


def test_start_reports_a_job_that_could_not_be_started(monkeypatch):
    """A failed trigger gives the container up at once (the job never sits in
    'surveying' waiting for a Job that is not running)."""
    from tests.test_source_designation import UuidFakeSession as Base

    class Session(Base):
        async def get(self, model, key, **kw):
            if model.__name__ == "IngestJob":
                return next(o for o in self.added if type(o).__name__ == "IngestJob")
            return await super().get(model, key, **kw)

        async def rollback(self):
            pass

    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [BIG])
    monkeypatch.setattr(task_service, "enqueue_ingest_batch", lambda **kw: None)

    def boom(j, p):
        raise RuntimeError("mailbox unpack job could not be started (HTTP 403): denied")

    monkeypatch.setattr(pst_explode, "trigger_explode_job", boom)
    db = Session(get_objects={("Production", 1): FakeProduction()})
    asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native"},
        background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    job = db.added[0]
    assert job.status == "failed"
    assert job.field_mapping["survey_failed"][BIG["storage_path"]].startswith("RuntimeError")
    errors = [m for p in db.executed_params if p and "new" in p for m in json.loads(p["new"])]
    assert any(m.startswith("big.pst: mailbox could not be read (the unpack job could not be started)")
               for m in errors)


def test_start_without_large_container_stays_processing(monkeypatch):
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [SMALL])
    monkeypatch.setattr(task_service, "enqueue_ingest_batch", lambda **kw: None)
    surveys = []
    monkeypatch.setattr(pst_explode, "trigger_explode_job", lambda j, p: surveys.append(p))
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

    asyncio.run(run_survey(db, "j1", "a.pst", _work))
    assert job.status == "surveying" and set(job.survey) == {"a.pst"}
    asyncio.run(run_survey(db, "j1", "b.pst", _work))
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
    asyncio.run(run_survey(db, "j1", "b.pst", _work))
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
        if "survey_progress" in sql:
            if job.status != "surveying":
                return FakeResult(rowcount=0)
            fm = dict(job.field_mapping)
            fm["survey_progress"] = {**(fm.get("survey_progress") or {}), params["path"]: params["at"]}
            job.field_mapping = fm
            self.progress_writes = getattr(self, "progress_writes", 0) + 1
            return FakeResult(rowcount=1)
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


class Work:
    """The Job's unpack work: fails for ``bad`` containers."""

    def __init__(self, bad=("a.pst",)):
        self.bad = bad
        self.calls = []

    def __call__(self, p):
        self.calls.append(p)
        if p in self.bad:
            raise OSError("bad block at offset 4096")
        return {"storage_path": p, "folders": []}


def test_max_survey_attempts():
    assert MAX_SURVEY_ATTEMPTS == 3


def test_survey_failing_three_times_fails_the_job(monkeypatch, deleted):
    path = "productions/1/raw/a.pst"
    job = _survey_job([path])
    job.production_id = 1
    db = SurveySession(job)
    work = Work(bad=(path,))
    for _ in range(MAX_SURVEY_ATTEMPTS - 1):
        with pytest.raises(OSError):
            asyncio.run(run_survey(db, "j1", path, work))
        assert job.status == "surveying" and job.errors == []
    # Third failure: given up.
    assert asyncio.run(run_survey(db, "j1", path, work)) == "gave_up"
    assert job.status == "failed" and job.completed_at is not None
    (msg,) = job.errors
    assert msg.startswith("a.pst: mailbox could not be read (survey failed 3 times): ")
    assert "bad block" in msg
    assert job.skipped_keys == [large_source_key(path)]
    assert job.field_mapping["survey_failed"][path]
    # The job failed outright: everything it exploded is deleted.
    assert deleted == [pst_explode.explode_prefix(1, "j1", path), "productions/1/exploded/j1/"]
    # A rerun does no work.
    assert asyncio.run(run_survey(db, "j1", path, work)) == "settled"
    assert len(work.calls) == MAX_SURVEY_ATTEMPTS and len(job.errors) == 1


def test_final_job_attempt_gives_up_below_the_attempt_cap():
    """The Job has fewer runs than MAX_SURVEY_ATTEMPTS: its last run gives up
    instead of leaving the job 'surveying' with nobody to retry."""
    job = _survey_job(["a.pst"])
    db = SurveySession(job)
    assert asyncio.run(run_survey(db, "j1", "a.pst", Work(), give_up_on_failure=True)) == "gave_up"
    assert job.status == "failed"
    assert job.errors[0].startswith("a.pst: mailbox could not be read (survey failed 1 times): ")


def test_survey_attempts_from_dead_workers_count(monkeypatch):
    """Attempts that died without raising (OOM/timeout) still count: the next
    claim past the cap gives up without walking the container again."""
    job = _survey_job(["a.pst"])
    job.field_mapping["survey_attempts"] = {"a.pst": MAX_SURVEY_ATTEMPTS}
    work = Work(bad=())
    asyncio.run(run_survey(SurveySession(job), "j1", "a.pst", work))
    assert work.calls == []
    assert job.status == "failed"
    assert job.errors == ["a.pst: mailbox could not be read (survey failed 3 times): "
                          "the worker stopped before the survey finished"]


def test_mixed_job_continues_without_the_failed_container(monkeypatch):
    job = _survey_job(["a.pst", "b.pst"])
    db = SurveySession(job)
    work = Work(bad=("a.pst",))
    asyncio.run(run_survey(db, "j1", "b.pst", work))
    assert job.status == "surveying"
    for _ in range(MAX_SURVEY_ATTEMPTS - 1):
        with pytest.raises(OSError):
            asyncio.run(run_survey(db, "j1", "a.pst", work))
    asyncio.run(run_survey(db, "j1", "a.pst", work))
    # b.pst still goes to selection; a.pst is reported and skipped.
    assert job.status == "awaiting_selection"
    assert set(job.survey) == {"b.pst"}
    assert len(job.errors) == 1 and job.errors[0].startswith("a.pst: mailbox could not be read")
    assert job.skipped_keys == [large_source_key("a.pst")]


def test_failed_container_with_normal_files_goes_on_to_finalize(monkeypatch):
    job = _survey_job(["a.pst"], total_files=3)
    db = SurveySession(job)
    finalized = []

    async def fake_finalize(db, job_id):
        finalized.append(job_id)

    monkeypatch.setattr(large_ingest, "maybe_finalize", fake_finalize)
    job.field_mapping["survey_attempts"] = {"a.pst": MAX_SURVEY_ATTEMPTS - 1}
    asyncio.run(run_survey(db, "j1", "a.pst", Work()))
    assert job.status == "processing" and finalized == ["j1"]



# --- Unit 8: the explode Job entry point -------------------------------------


class _Factory:
    """async_session_factory stand-in: every session sees the same job."""

    def __init__(self, job, broken_sessions=0):
        self.job = job
        self.broken = broken_sessions
        self.opened = 0

    def __call__(self):
        factory = self

        class Ctx:
            async def __aenter__(self):
                factory.opened += 1
                if factory.broken:
                    factory.broken -= 1
                    raise OSError("connection refused")
                return SurveySession(factory.job)

            async def __aexit__(self, *a):
                return False

        return Ctx()


def _job_run(job, work, final=False, broken=0):
    from app.jobs import explode_pst

    factory = _Factory(job, broken)
    code = asyncio.run(explode_pst.run("j1", "a.pst", final_attempt=final,
                                       session_factory=factory, work=work))
    return code, factory


def test_job_records_the_survey_and_exits_zero():
    job = _survey_job(["a.pst"])
    job.production_id = 1
    code, _ = _job_run(job, Work(bad=()))
    assert code == 0 and job.status == "awaiting_selection" and set(job.survey) == {"a.pst"}


def test_job_failure_before_its_last_attempt_exits_nonzero_for_a_retry():
    job = _survey_job(["a.pst"])
    job.production_id = 1
    code, _ = _job_run(job, Work())
    assert code == 1 and job.status == "surveying" and job.errors == []


def test_job_failure_on_its_last_attempt_records_the_error_and_exits_nonzero():
    job = _survey_job(["a.pst"])
    job.production_id = 1
    code, _ = _job_run(job, Work(), final=True)
    assert code == 1 and job.status == "failed"
    assert job.errors[0].startswith("a.pst: mailbox could not be read")


def test_job_database_failure_before_the_counter_is_recorded_on_the_last_attempt():
    """Ruling 5: a DB failure before the attempt counter is written gets the
    same capped handling: on the final run it is recorded on the job."""
    job = _survey_job(["a.pst"])
    job.production_id = 1
    work = Work(bad=())
    code, factory = _job_run(job, work, final=True, broken=1)
    assert code == 1 and work.calls == []
    assert job.status == "failed" and "connection refused" in job.errors[0]
    # Not the last attempt: no record, the retry will try again.
    job2 = _survey_job(["a.pst"])
    job2.production_id = 1
    code, _ = _job_run(job2, Work(bad=()), final=False, broken=1)
    assert code == 1 and job2.status == "surveying" and job2.errors == []


def test_job_main_parses_args_and_the_attempt(monkeypatch):
    from app.jobs import explode_pst

    seen = {}

    async def fake_run(job_id, storage_path, *, final_attempt, **kw):
        seen.update(job_id=job_id, storage_path=storage_path, final=final_attempt)
        return 1

    monkeypatch.setattr(explode_pst, "run", fake_run)
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "2")
    with pytest.raises(SystemExit) as e:
        explode_pst.main(["--job-id", "j9", "--storage-path", "productions/1/raw/a.pst"])
    assert e.value.code == 1
    assert seen == {"job_id": "j9", "storage_path": "productions/1/raw/a.pst", "final": True}
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    with pytest.raises(SystemExit):
        explode_pst.main(["--job-id", "j9", "--storage-path", "x"])
    assert seen["final"] is False



# --- Fix round 1 --------------------------------------------------------------


def test_job_retries_line_up_with_the_attempt_cap():
    assert pst_explode.EXPLODE_JOB_MAX_RETRIES == 2
    assert pst_explode.EXPLODE_JOB_MAX_RETRIES + 1 == MAX_SURVEY_ATTEMPTS


def test_permanent_failure_gives_up_at_once_without_retrying(deleted):
    job = _survey_job(["a.pst"])
    job.production_id = 1
    db = SurveySession(job)

    def too_big(p):
        raise pst_explode.PermanentSurveyFailure(
            "a.pst: too large for in-memory unpack (32.2 GB); contact support")

    assert asyncio.run(run_survey(db, "j1", "a.pst", too_big)) == "refused"
    assert job.errors == ["a.pst: too large for in-memory unpack (32.2 GB); contact support"]
    assert job.status == "failed" and "a.pst" in job.field_mapping["survey_failed"]
    assert job.skipped_keys == [large_source_key("a.pst")]


def test_job_exits_zero_after_a_permanent_refusal():
    job = _survey_job(["a.pst"])
    job.production_id = 1

    def too_big(p):
        raise pst_explode.PermanentSurveyFailure("a.pst: too large for in-memory unpack (32.2 GB); contact support")

    code, _ = _job_run(job, too_big)
    assert code == 0 and job.status == "failed"


def test_start_refuses_an_oversized_container_without_starting_a_job(monkeypatch):
    from tests.test_source_designation import UuidFakeSession as Base

    class Session(Base):
        async def get(self, model, key, **kw):
            if model.__name__ == "IngestJob":
                return next(o for o in self.added if type(o).__name__ == "IngestJob")
            return await super().get(model, key, **kw)

        async def rollback(self):
            pass

    huge = dict(BIG, size=30 * 1024 ** 3)
    started = []
    monkeypatch.setattr(task_service, "is_configured", lambda: True)
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [SMALL, huge])
    monkeypatch.setattr(task_service, "enqueue_ingest_batch", lambda **kw: None)
    monkeypatch.setattr(pst_explode, "trigger_explode_job", lambda j, p: started.append(p))
    db = Session(get_objects={("Production", 1): FakeProduction()})
    asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native"},
        background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    job = db.added[0]
    assert started == []
    errors = [m for p in db.executed_params if p and "new" in p for m in json.loads(p["new"])]
    assert errors == ["big.pst: too large for in-memory unpack (32.2 GB); contact support"]
    assert job.status == "processing"  # the small file still loads, then finalize


def test_job_writes_progress_at_start_and_while_items_advance(monkeypatch):
    import time as _time

    from app.jobs import explode_pst

    monkeypatch.setattr(explode_pst, "HEARTBEAT_S", 0.01)
    job = _survey_job(["a.pst"])
    job.production_id = 1
    sessions = []

    class Factory(_Factory):
        def __call__(self):
            ctx = super().__call__()
            outer = self

            class Wrap:
                async def __aenter__(self):
                    s = await ctx.__aenter__()
                    sessions.append(s)
                    return s

                async def __aexit__(self, *a):
                    return False

            return Wrap()

    def work(p, progress):
        for _ in range(5):
            progress.bump()
            _time.sleep(0.03)
        return {"storage_path": p, "folders": []}

    code = asyncio.run(explode_pst.run("j1", "a.pst", final_attempt=False,
                                       session_factory=Factory(job), work=work))
    assert code == 0 and job.status == "awaiting_selection"
    writes = sum(getattr(s, "progress_writes", 0) for s in sessions)
    assert writes >= 2
    assert "a.pst" in job.field_mapping["survey_progress"]


def test_a_run_given_up_on_while_working_does_not_record_its_survey():
    """The stall self-heal gave the container up while a slow run was still
    working: its late result is discarded, not recorded."""
    job = _survey_job(["a.pst", "b.pst"])
    db = SurveySession(job)

    def slow(p):
        job.field_mapping = {**job.field_mapping, "survey_failed": {p: "no progress for 4.5 h"}}
        return {"storage_path": p, "folders": []}

    assert asyncio.run(run_survey(db, "j1", "a.pst", slow)) == "discarded"
    assert job.survey == {}
