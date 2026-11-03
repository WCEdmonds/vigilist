"""Task 5: estimate + uploader selection -> chunk planning and fan-out. No DB."""

import asyncio
import itertools
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routers.ingest as ri
import app.services.ingest_native as native_mod
import app.services.tasks as task_service
from app.services import large_ingest
from app.services.large_ingest import CHUNK_ITEMS, SelectionError, apply_selection, plan_chunks
from app.services.large_ingest_estimate import estimate_load
from app.services.pst_reader import ContainerSurvey, FolderSurvey
from tests.fakes import FakeSession, FakeUser

BIG = "productions/1/raw/loads/L1/acme.pst"


def _folder(index, path, runs, recoverable=False, item_bytes=1000):
    counts, sizes = {}, {}
    for kind, n in runs:
        counts[kind] = counts.get(kind, 0) + n
        sizes[kind] = sizes.get(kind, 0) + n * item_bytes
    return FolderSurvey(index, path, recoverable, counts, sizes, [list(r) for r in runs])


@pytest.fixture
def sample_survey():
    return ContainerSurvey(BIG, [
        _folder(0, "", []),
        _folder(1, "Top/Inbox", [("email", 5)]),
        _folder(2, "Top/Calendar", [("calendar", 4)]),
        _folder(3, "Top/Contacts", [("contact", 3)]),
        _folder(4, "Top/Mixed", [("email", 2), ("contact", 1), ("email", 2), ("calendar", 1), ("note", 2)]),
        _folder(5, "Top/Recoverable Items", [("email", 2)], recoverable=True),
    ])


@pytest.fixture
def big_survey():
    # ~75k emails over 11.2 GB, the incident mailbox's shape (synthetic).
    folders = [_folder(0, "", [])]
    for i in range(15):
        folders.append(_folder(i + 1, f"Top/Folder{i}", [("email", 5_000)], item_bytes=149_000))
    folders.append(_folder(16, "Top/Contacts", [("contact", 900)], item_bytes=5_000))
    return ContainerSurvey(BIG, folders)


def sample_survey_total(survey, types, include_recoverable):
    return sum(
        n for f in survey.folders if include_recoverable or not f.recoverable
        for t, n in f.counts.items() if t in types
    )


def _selected_positions(survey, types, include_recoverable):
    out = []
    for f in survey.folders:
        if f.recoverable and not include_recoverable:
            continue
        pos = 0
        for kind, n in f.runs:
            if kind in types:
                out.extend((f.index, p) for p in range(pos, pos + n))
            pos += n
    return out


def _expand(survey, chunk):
    """Positions a chunk reads: its raw slices, filtered by its type_filter."""
    kinds = {}
    for f in survey.folders:
        pos = 0
        for kind, n in f.runs:
            for p in range(pos, pos + n):
                kinds[(f.index, p)] = kind
            pos += n
    flt = set(chunk.get("type_filter") or [])
    out = []
    for fi, s, e in chunk["slices"]:
        for p in range(s, e):
            if not flt or kinds[(fi, p)] in flt:
                out.append((fi, p))
    return out


# --- plan_chunks -----------------------------------------------------------


def test_plan_chunks_covers_selected_items_exactly_once(sample_survey):
    chunks = plan_chunks(sample_survey, {"email", "calendar"}, include_recoverable=True, chunk_items=3)
    assert sum(c["item_count"] for c in chunks) == sample_survey_total(sample_survey, {"email", "calendar"}, True)
    assert [c["global_offset"] for c in chunks] == list(itertools.accumulate([0] + [c["item_count"] for c in chunks[:-1]]))
    read = [p for c in chunks for p in _expand(sample_survey, c)]
    assert read == _selected_positions(sample_survey, {"email", "calendar"}, True)
    for c in chunks:
        assert len(_expand(sample_survey, c)) == c["item_count"] <= 3


def test_plan_chunks_excludes_recoverable_when_unchecked(sample_survey):
    chunks = plan_chunks(sample_survey, {"email"}, include_recoverable=False, chunk_items=100)
    rec = {f.index for f in sample_survey.folders if f.recoverable}
    assert all(s[0] not in rec for c in chunks for s in c["slices"])
    assert sum(c["item_count"] for c in chunks) == 9  # 5 inbox + 4 mixed


def test_plan_chunks_sets_type_filter_only_where_a_slice_spans_unselected_items(sample_survey):
    chunks = plan_chunks(sample_survey, {"email"}, include_recoverable=True, chunk_items=100)
    assert len(chunks) == 1
    (chunk,) = chunks
    assert chunk["type_filter"] == ["email"]  # Mixed folder: email, contact, email
    assert [4, 0, 5] in chunk["slices"]
    pure = plan_chunks(sample_survey, {"calendar"}, include_recoverable=True, chunk_items=2)
    assert [c["slices"] for c in pure] == [[[2, 0, 2]], [[2, 2, 4]], [[4, 5, 6]]]
    assert all(not c.get("type_filter") for c in pure)


def test_plan_chunks_nothing_selected_is_empty(sample_survey):
    assert plan_chunks(sample_survey, set(), True, CHUNK_ITEMS) == []
    assert plan_chunks(sample_survey, {"task"}, True, CHUNK_ITEMS) == []


def test_plan_chunks_mixed_folder_without_positions_is_rejected():
    s = ContainerSurvey(BIG, [FolderSurvey(0, "Top/Inbox", False, {"email": 3, "contact": 1}, {})])
    with pytest.raises(ValueError):
        plan_chunks(s, {"email"}, True, 10)
    # A pure folder needs no positions.
    pure = ContainerSurvey(BIG, [FolderSurvey(0, "Top/Inbox", False, {"email": 3}, {})])
    assert plan_chunks(pure, {"email"}, True, 10) == [
        {"slices": [[0, 0, 3]], "global_offset": 0, "item_count": 3}
    ]


def test_chunk_items_default_is_provisional_500():
    assert CHUNK_ITEMS == 500


# --- estimate_load ----------------------------------------------------------


def test_estimate_flags_over_budget(big_survey):
    assert estimate_load(big_survey, {"email"}, True)["over_budget"] is False   # ~75k emails ≈ $35–80
    assert set(estimate_load(big_survey, {"email"}, True)) >= {"items", "minutes", "usd", "storage_gb", "over_budget"}


def test_estimate_values_and_over_budget_flag(big_survey):
    est = estimate_load(big_survey, {"email"}, True)
    assert est["items"] == 75_000
    assert est["bytes"] == 75_000 * 149_000
    assert 35 <= est["usd"] <= 80
    assert est["minutes"] > 0 and est["storage_gb"] > 10
    huge = ContainerSurvey(BIG, [_folder(0, "Top", [("email", 1_000_000)], item_bytes=150_000)])
    assert estimate_load(huge, {"email"}, True)["over_budget"] is True


def test_estimate_respects_types_and_recoverable(sample_survey):
    assert estimate_load(sample_survey, {"email"}, False)["items"] == 9
    assert estimate_load(sample_survey, {"email"}, True)["items"] == 11
    assert estimate_load(sample_survey, set(), True)["usd"] == 0


# --- apply_selection --------------------------------------------------------


def _job(survey, *, status="awaiting_selection", containers=(BIG,), selection=None):
    return SimpleNamespace(
        id="j1", user_id="u1", production_id=1, status=status,
        survey={survey.storage_path: survey.to_json()},
        selection=dict(selection or {}),
        items_total=0, errors=[],
        field_mapping={"large_containers": list(containers), "control_offset": 4,
                       "load_prefix": "loads/L1/"},
    )


SOURCES = [
    {"storage_path": "productions/1/raw/loads/L1/a.eml", "relative_path": "a.eml", "filename": "a.eml", "size": 0},
    {"storage_path": BIG, "relative_path": "acme.pst", "filename": "acme.pst", "size": 11_200_000_000},
]


@pytest.fixture
def env(monkeypatch):
    enq, fin = [], []
    monkeypatch.setattr(task_service, "enqueue_chunk", lambda cid: enq.append(cid))
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: SOURCES)

    async def fake_finalize(db, job_id):
        fin.append(job_id)

    monkeypatch.setattr(large_ingest, "maybe_finalize", fake_finalize)
    return SimpleNamespace(enqueued=enq, finalized=fin)


class RollbackSession(FakeSession):
    rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


def _db(job):
    return RollbackSession(get_objects={
        ("IngestJob", "j1"): job,
        ("Production", 1): SimpleNamespace(id=1, name="Acme Corp"),
    })


def test_apply_selection_writes_chunks_selection_and_enqueues(sample_survey, env, monkeypatch):
    monkeypatch.setattr(large_ingest, "CHUNK_ITEMS", 3)
    job = _job(sample_survey)
    db = _db(job)
    est = asyncio.run(apply_selection(db, "j1", BIG, ["email", "calendar"], True))

    chunks = [o for o in db.added if type(o).__name__ == "IngestChunk"]
    assert sum(c.item_count for c in chunks) == 16 == job.items_total
    assert all(c.kind == "items" and c.status == "pending" and c.container_path == BIG for c in chunks)
    assert env.enqueued == [str(c.id) for c in chunks]
    assert job.status == "processing"
    sel = job.selection[BIG]
    assert sel["types"] == ["calendar", "email"] and sel["include_recoverable"] is True
    # Container control from its list_native_sources position (index 1) + offset 4.
    assert sel["control"] == "ACME 000006" and sel["items"] == 16
    assert sel["relative_path"] == "acme.pst"
    assert est["items"] == 16 and "usd" in est
    assert env.finalized == []
    assert db.commits >= 1


def test_apply_selection_twice_is_409(sample_survey, env):
    job = _job(sample_survey, selection={BIG: {"types": ["email"], "include_recoverable": True}})
    db = _db(job)
    with pytest.raises(SelectionError) as e:
        asyncio.run(apply_selection(db, "j1", BIG, ["email"], True))
    assert e.value.status == 409
    assert db.rollbacks == 1  # row lock released before raising


def test_apply_selection_survey_without_positions_is_409(env):
    legacy = ContainerSurvey(BIG, [FolderSurvey(0, "Top/Inbox", False, {"email": 3, "contact": 1}, {})])
    job = _job(legacy)
    db = _db(job)
    with pytest.raises(SelectionError) as e:
        asyncio.run(apply_selection(db, "j1", BIG, ["email"], True))
    assert e.value.status == 409 and "re-survey" in e.value.detail
    assert db.rollbacks == 1
    assert job.selection == {} and env.enqueued == []


def test_apply_selection_requires_awaiting_selection(sample_survey, env):
    job = _job(sample_survey, status="surveying")
    with pytest.raises(SelectionError) as e:
        asyncio.run(apply_selection(_db(job), "j1", BIG, ["email"], True))
    assert e.value.status == 409


def test_apply_selection_unknown_container_is_404(sample_survey, env):
    job = _job(sample_survey)
    with pytest.raises(SelectionError) as e:
        asyncio.run(apply_selection(_db(job), "j1", "productions/1/raw/other.pst", ["email"], True))
    assert e.value.status == 404


def test_apply_selection_waits_for_every_large_container(sample_survey, env):
    other = "productions/1/raw/loads/L1/zeta.pst"
    job = _job(sample_survey, containers=(BIG, other))
    asyncio.run(apply_selection(_db(job), "j1", BIG, ["email"], True))
    # Still awaiting the second container's selection; chunks already queued.
    assert job.status == "awaiting_selection"
    assert env.enqueued


def test_empty_selection_completes_immediately(sample_survey, env):
    # types=[] -> no chunks, items_total=0, job proceeds to finalize without
    # hanging in "processing" waiting on chunks that will never exist.
    job = _job(sample_survey)
    db = _db(job)
    est = asyncio.run(apply_selection(db, "j1", BIG, [], True))
    assert [o for o in db.added if type(o).__name__ == "IngestChunk"] == []
    assert job.items_total == 0 and est["items"] == 0
    assert env.enqueued == []
    assert env.finalized == ["j1"]
    assert job.selection[BIG]["types"] == []


# --- router -----------------------------------------------------------------


def test_selection_endpoint_rejects_unknown_type(sample_survey, env):
    job = _job(sample_survey)
    with pytest.raises(HTTPException) as e:
        asyncio.run(ri.apply_container_selection(
            job_id="j1", body={"storage_path": BIG, "types": ["email", "spam"]},
            db=_db(job), user=FakeUser("u1")))
    assert e.value.status_code == 422


def test_selection_endpoint_is_owner_only(sample_survey, env):
    job = _job(sample_survey)
    with pytest.raises(HTTPException) as e:
        asyncio.run(ri.apply_container_selection(
            job_id="j1", body={"storage_path": BIG, "types": ["email"]},
            db=_db(job), user=FakeUser("intruder")))
    assert e.value.status_code == 403


def test_selection_endpoint_maps_conflict_to_409(sample_survey, env):
    job = _job(sample_survey, status="processing")
    with pytest.raises(HTTPException) as e:
        asyncio.run(ri.apply_container_selection(
            job_id="j1", body={"storage_path": BIG, "types": ["email"]},
            db=_db(job), user=FakeUser("u1")))
    assert e.value.status_code == 409


def test_selection_endpoint_returns_estimate(sample_survey, env):
    job = _job(sample_survey)
    out = asyncio.run(ri.apply_container_selection(
        job_id="j1", body={"storage_path": BIG, "types": ["email"], "include_recoverable": False},
        db=_db(job), user=FakeUser("u1")))
    assert out["estimate"]["items"] == 9
    assert job.selection[BIG]["include_recoverable"] is False


def test_enqueue_chunk_targets_process_chunk(monkeypatch):
    calls = []
    monkeypatch.setattr(task_service, "_enqueue", lambda path, body, **kw: calls.append((path, body)))
    task_service.enqueue_chunk("c1")
    assert calls == [("/api/ingest/process-chunk", {"chunk_id": "c1"})]


def test_apply_selection_counts_a_failed_survey_as_settled(sample_survey, env):
    """C1: a container given up on during survey must not hold the job in
    awaiting_selection after the other container's selection."""
    job = _job(sample_survey, containers=(BIG, "productions/1/raw/loads/L1/bad.pst"))
    job.field_mapping["survey_failed"] = {"productions/1/raw/loads/L1/bad.pst": "OSError: x"}
    asyncio.run(apply_selection(_db(job), "j1", BIG, ["email"], True))
    assert job.status == "processing"
