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
    db = UuidFakeSession(get_objects={("IngestJob", "j1"): job})
    monkeypatch.setattr(large_ingest, "_survey_container",
                        lambda p: {"storage_path": p, "folders": []})

    asyncio.run(run_survey(db, "j1", "a.pst"))
    assert job.status == "surveying" and set(job.survey) == {"a.pst"}
    asyncio.run(run_survey(db, "j1", "b.pst"))
    assert job.status == "awaiting_selection" and set(job.survey) == {"a.pst", "b.pst"}
