"""Task 7: atomic finalize, reconciliation, embed/post fan-out, completion.

A small in-memory simulation of the statements the finalize path issues (no
DB, no GCS, no Voyage, no Anthropic). Synthetic ACME data only.
"""

import asyncio
import json
import math
import uuid
from types import SimpleNamespace

import pytest

import app.services.ingest as ingest_mod
import app.services.ingest_native as native_mod
import app.services.tasks as task_service
from app.config import settings
from app.models import IngestChunk
from app.services import embeddings, email_threading, large_ingest, large_ingest_chunk
from app.services import large_ingest_finalize as fin
from app.services.large_ingest import large_source_key, maybe_finalize
from app.services.large_ingest_chunk import MAX_CHUNK_ATTEMPTS, run_chunk
from tests.fakes import FakeResult, FakeSession

PST = "productions/1/raw/big.pst"
CONTROL = "ACME 000002"


def _survey(counts, recoverable_counts=None):
    folders = [{"index": 1, "path": "Top/Inbox", "recoverable": False, "counts": counts,
                "bytes": {}, "runs": []}]
    if recoverable_counts:
        folders.append({"index": 2, "path": "Top/Deleted Items", "recoverable": True,
                        "counts": recoverable_counts, "bytes": {}, "runs": []})
    return {"storage_path": PST, "folders": folders}


class State:
    """Shared 'database' for one or more SimSessions."""

    def __init__(self, *, selected=10, loaded=None, types=("email",), counts=None,
                 recoverable_counts=None, include_recoverable=True, embed_docs=(),
                 total_files=1, done_keys=(), status="processing"):
        counts = counts or {"email": 10, "contact": 4}
        self.job = SimpleNamespace(
            id="j1", production_id=1, status=status, source_format="native",
            total_files=total_files, done_keys=list(done_keys), skipped_keys=[],
            errors=[], completed_at=None,
            field_mapping={"large_containers": [PST], "custodian": "Alice Example"},
            survey={PST: _survey(counts, recoverable_counts)},
            selection={PST: {"types": list(types), "include_recoverable": include_recoverable,
                             "control": CONTROL, "relative_path": "big.pst",
                             "items": selected}},
            items_total=selected, items_done=0, items_failed=0,
        )
        self.production = SimpleNamespace(id=1, name="ACME v. Example", ai_hold=False)
        self.loaded = selected if loaded is None else loaded
        self.embed_docs = [str(d) for d in embed_docs]
        self.embedded: set[str] = set()
        self.chunks: dict[str, object] = {}
        self.claims_finalizing = 0
        self.pipeline = []
        self.enqueued = []
        self.orphans = []

    def add_chunk(self, kind, status, **kw):
        cid = str(uuid.uuid4())
        self.chunks[cid] = SimpleNamespace(
            id=cid, job_id="j1", kind=kind, status=status, attempts=kw.pop("attempts", 0),
            container_path=kw.pop("container_path", PST if kind == "items" else None),
            slices=kw.pop("slices", []), type_filter=[], global_offset=0,
            item_count=kw.pop("item_count", 1), doc_ids=kw.pop("doc_ids", []),
            parent_id=kw.pop("parent_id", None), error=None,
        )
        return cid

    def of_kind(self, kind):
        return [c for c in self.chunks.values() if c.kind == kind]


class SimSession(FakeSession):
    def __init__(self, state: State):
        super().__init__()
        self.s = state
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1

    async def get(self, model, key, **_kw):
        name = model.__name__
        if name == "IngestJob":
            return self.s.job if str(key) == "j1" else None
        if name == "IngestChunk":
            return self.s.chunks.get(str(key))
        if name == "Production":
            return self.s.production
        return None

    def add(self, obj):
        if isinstance(obj, IngestChunk):
            obj.id = obj.id or uuid.uuid4()
            obj.job_id = str(obj.job_id)
            self.s.chunks[str(obj.id)] = obj
        self.added.append(obj)

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        self.executed.append(sql)
        self.executed_params.append(params)
        s, job = self.s, self.s.job
        if "GROUP BY kind, status" in sql:
            counts: dict = {}
            for c in s.chunks.values():
                counts[(c.kind, c.status)] = counts.get((c.kind, c.status), 0) + 1
            return FakeResult(rows=[(k, st, n) for (k, st), n in counts.items()])
        if "SKIP LOCKED" in sql:
            return FakeResult(rows=[(o,) for o in s.orphans])
        if "SET status = 'finalizing'" in sql:
            await asyncio.sleep(0)  # let a concurrent caller interleave
            if job.status == "processing":
                job.status = "finalizing"
                s.claims_finalizing += 1
                return FakeResult(rowcount=1)
            return FakeResult(rowcount=0)
        if "done_keys = done_keys ||" in sql:
            if params["key"] not in job.done_keys:
                job.done_keys.append(params["key"])
            return FakeResult(rowcount=1)
        if "family_id = bates_begin" in sql:
            return FakeResult(scalar=s.loaded)
        if "errors = errors ||" in sql:
            job.errors.extend(json.loads(params["new"]))
            return FakeResult(rowcount=1)
        if "SET ai_hold = true" in sql:
            s.production.ai_hold = True
            return FakeResult(rowcount=1)
        if "FROM documents d" in sql:
            return FakeResult(rows=[(uuid.UUID(d),) for d in s.embed_docs])
        if "status = 'finalizing' FOR UPDATE" in sql:
            await asyncio.sleep(0)
            return FakeResult(items=[job.id] if job.status == "finalizing" else [])
        if "INSERT INTO ingest_chunks" in sql:
            if s.of_kind("post"):
                return FakeResult(scalar=None)
            cid = s.add_chunk("post", "pending", item_count=0)
            return FakeResult(scalar=cid)
        if "RETURNING attempts" in sql:
            c = s.chunks.get(str(params["id"]))
            if c is None or c.status not in ("pending", "running"):
                return FakeResult(scalar=None)
            c.attempts += 1
            c.status = "running"
            return FakeResult(scalar=c.attempts)
        for target in ("done", "split", "failed"):
            if f"SET status = '{target}'" in sql and "ingest_chunks" in sql:
                c = s.chunks.get(str(params["id"]))
                if c is not None and c.status == "running":
                    c.status = target
                    c.error = params.get("err")
                    return FakeResult(rowcount=1)
                return FakeResult(rowcount=0)
        if "FROM document_chunks WHERE document_id" in sql:
            return FakeResult(items=[1] if str(params["id"]) in s.embedded else [])
        if "SELECT documents.bates_begin" in sql or "bates_begin FROM documents" in sql:
            return FakeResult(scalar="ACME 000002 -0001")
        if "SET status = 'complete'" in sql:
            if job.status == "finalizing":
                job.status = "complete"
                job.completed_at = params.get("now")
                return FakeResult(rowcount=1)
            return FakeResult(rowcount=0)
        if "SET items_" in sql or "items_failed = items_failed" in sql:
            return FakeResult(rowcount=1)
        return FakeResult()


@pytest.fixture
def env(monkeypatch):
    holder = {}

    def make(**kw):
        s = State(**kw)
        holder["s"] = s
        monkeypatch.setattr(task_service, "enqueue_chunk", lambda cid: s.enqueued.append(str(cid)))
        monkeypatch.setattr(task_service, "enqueue_pipeline", lambda pid: s.pipeline.append(pid))
        monkeypatch.setattr(task_service, "is_configured", lambda: True)
        monkeypatch.setattr(settings, "voyage_api_key", "test-key")

        async def fake_embed(db, doc_id):
            s.embedded.add(str(doc_id))
            return 1

        monkeypatch.setattr(embeddings, "chunk_and_embed_document", fake_embed)
        s.threads = []
        s.titled = []

        async def fake_threads(db, production_id):
            s.threads.append(production_id)

        async def fake_titles(db, production_id, errors, **kw):
            s.titled.append((production_id, kw))

        monkeypatch.setattr(email_threading, "derive_threads", fake_threads)
        monkeypatch.setattr(ingest_mod, "generate_missing_titles", fake_titles)
        return s

    return make


def _finalize(s):
    asyncio.run(maybe_finalize(SimSession(s), "j1"))


def _run(s, cid):
    asyncio.run(run_chunk(SimSession(s), cid))


def _drain(s, kind):
    for c in list(s.of_kind(kind)):
        if c.status == "pending":
            _run(s, str(c.id))


def test_embed_batch_docs():
    assert fin.EMBED_BATCH_DOCS == 200


def test_finalize_claims_once(env):
    docs = [uuid.uuid4() for _ in range(450)]
    s = env(embed_docs=docs)
    s.add_chunk("items", "done")

    async def both():
        await asyncio.gather(maybe_finalize(SimSession(s), "j1"), maybe_finalize(SimSession(s), "j1"))

    asyncio.run(both())
    assert s.claims_finalizing == 1
    embeds = s.of_kind("embed")
    assert len(embeds) == math.ceil(450 / 200)
    assert sorted(len(c.doc_ids) for c in embeds) == [50, 200, 200]
    assert {d for c in embeds for d in c.doc_ids} == {str(d) for d in docs}
    assert sorted(s.enqueued) == sorted(str(c.id) for c in embeds)
    assert len([e for e in s.job.errors if e.startswith("big.pst:")]) == 1


def test_finalize_claim_is_one_conditional_update(env):
    s = env()
    db = SimSession(s)
    asyncio.run(maybe_finalize(db, "j1"))
    claims = [q for q in db.executed if "SET status = 'finalizing'" in q]
    assert claims == [fin._CLAIM_FINALIZE_SQL]
    assert "WHERE id = :id AND status = 'processing'" in claims[0]


def test_finalize_waits_for_pending_items(env):
    s = env()
    s.add_chunk("items", "done")
    s.add_chunk("items", "running")
    db = SimSession(s)
    asyncio.run(maybe_finalize(db, "j1"))
    assert s.job.status == "processing"
    assert not any("SET status = 'finalizing'" in q for q in db.executed)
    assert s.of_kind("embed") == [] and s.job.errors == []


def test_finalize_waits_for_split_children_then_proceeds(env):
    s = env()
    parent = s.add_chunk("items", "split", item_count=2)
    child = s.add_chunk("items", "pending", parent_id=parent)
    s.add_chunk("items", "done", parent_id=parent)
    _finalize(s)
    assert s.job.status == "processing"
    s.chunks[child].status = "failed"
    _finalize(s)
    assert s.job.status in ("finalizing", "complete") and s.claims_finalizing == 1


def test_finalize_waits_for_normal_sources(env):
    # 1 large container + 2 normal files; only one normal file attempted.
    s = env(total_files=3, done_keys=["native:productions/1/raw/a.txt"])
    s.add_chunk("items", "done")
    _finalize(s)
    assert s.job.status == "processing"
    s.job.skipped_keys = ["native:productions/1/raw/b.txt"]
    _finalize(s)
    assert s.claims_finalizing == 1


def test_large_container_key_does_not_count_as_normal_source(env):
    # The large key already marked must not stand in for a missing normal file.
    s = env(total_files=2, done_keys=[large_source_key(PST)])
    _finalize(s)
    assert s.job.status == "processing"


def test_large_source_keys_marked_done(env):
    s = env(total_files=2, done_keys=["native:productions/1/raw/a.txt"])
    s.add_chunk("items", "done")
    _finalize(s)
    assert large_source_key(PST) in s.job.done_keys


def test_reconciliation_line_lists_exclusions(env):
    s = env(selected=10, loaded=8, counts={"email": 10, "contact": 4})
    s.add_chunk("items", "done")
    _finalize(s)
    assert ("big.pst: 8 of 10 loaded, 2 could not be read; excluded by uploader: contact 4; "
            "deep recovery of orphaned items was not performed") in s.job.errors


def test_reconciliation_line_formats_thousands_and_recoverable(env):
    s = env(selected=12_000, loaded=11_994, counts={"email": 12_000, "calendar": 1_500, "task": 2},
            recoverable_counts={"email": 30}, include_recoverable=False)
    s.add_chunk("items", "done")
    _finalize(s)
    assert ("big.pst: 11,994 of 12,000 loaded, 6 could not be read; excluded by uploader: "
            "calendar 1,500, task 2, recoverable items folders 30; "
            "deep recovery of orphaned items was not performed") in s.job.errors


def test_reconciliation_counts_only_this_containers_parent_messages(env):
    s = env()
    s.add_chunk("items", "done")
    db = SimSession(s)
    asyncio.run(maybe_finalize(db, "j1"))
    i = next(i for i, q in enumerate(db.executed) if "family_id = bates_begin" in q)
    assert db.executed_params[i]["pat"] == "ACME 000002 -%"
    assert db.executed_params[i]["pid"] == 1


def test_empty_selection_still_completes(env):
    s = env(selected=0, loaded=0, types=())
    _finalize(s)
    assert ("big.pst: 0 of 0 loaded, 0 could not be read; excluded by uploader: email 10, "
            "contact 4; deep recovery of orphaned items was not performed") in s.job.errors
    assert s.of_kind("embed") == []
    posts = s.of_kind("post")
    assert len(posts) == 1 and s.enqueued == [str(posts[0].id)]
    _drain(s, "post")
    assert s.job.status == "complete" and s.job.completed_at is not None


def test_complete_only_after_embed_and_post(env):
    docs = [uuid.uuid4() for _ in range(3)]
    s = env(embed_docs=docs)
    s.add_chunk("items", "done")
    _finalize(s)
    assert s.job.status == "finalizing"
    assert s.production.ai_hold is True
    assert s.of_kind("post") == []
    _drain(s, "embed")
    assert s.embedded == {str(d) for d in docs}
    assert s.job.status == "finalizing"
    posts = s.of_kind("post")
    assert len(posts) == 1
    _drain(s, "post")
    assert s.job.status == "complete" and s.job.completed_at is not None
    assert s.threads == [1] and s.titled and s.titled[0][0] == 1
    assert s.pipeline == []


def test_post_chunk_created_once(env):
    s = env(status="finalizing")
    s.add_chunk("embed", "done", doc_ids=["x"])

    async def both():
        await asyncio.gather(maybe_finalize(SimSession(s), "j1"), maybe_finalize(SimSession(s), "j1"))

    asyncio.run(both())
    assert len(s.of_kind("post")) == 1
    assert len(s.enqueued) == 1


def test_post_insert_runs_under_job_row_lock(env):
    s = env(status="finalizing")
    db = SimSession(s)
    asyncio.run(maybe_finalize(db, "j1"))
    lock = next(i for i, q in enumerate(db.executed) if "FOR UPDATE" in q)
    insert = next(i for i, q in enumerate(db.executed) if "INSERT INTO ingest_chunks" in q)
    assert lock < insert
    assert "NOT EXISTS" in db.executed[insert] and "kind = 'post'" in db.executed[insert]


def test_phase_b_waits_for_embed_chunks(env):
    s = env(status="finalizing")
    s.add_chunk("embed", "running", doc_ids=["x"])
    _finalize(s)
    assert s.of_kind("post") == []


def test_embed_chunk_embeds_and_skips_already_embedded(env, monkeypatch):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    s = env(status="finalizing")
    s.embedded.add(a)
    cid = s.add_chunk("embed", "pending", doc_ids=[a, b], item_count=2)
    calls = []

    async def fake_embed(db, doc_id):
        calls.append(str(doc_id))
        s.embedded.add(str(doc_id))
        return 1

    monkeypatch.setattr(embeddings, "chunk_and_embed_document", fake_embed)
    _run(s, cid)
    assert calls == [b]
    assert s.chunks[cid].status == "done"
    assert len(s.of_kind("post")) == 1  # finalize ran after the embed chunk


def test_embed_failure_raises_for_retry_then_resumes(env, monkeypatch):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    s = env(status="finalizing")
    cid = s.add_chunk("embed", "pending", doc_ids=[a, b], item_count=2)
    fail = {b}

    async def flaky(db, doc_id):
        if str(doc_id) in fail:
            raise RuntimeError("voyage down")
        s.embedded.add(str(doc_id))
        return 1

    monkeypatch.setattr(embeddings, "chunk_and_embed_document", flaky)
    with pytest.raises(RuntimeError):
        _run(s, cid)
    assert s.chunks[cid].status == "running" and s.embedded == {a}
    fail.clear()
    _run(s, cid)
    assert s.chunks[cid].status == "done" and s.embedded == {a, b}


def test_embed_chunk_splits_after_max_attempts(env):
    s = env(status="finalizing")
    ids = [str(uuid.uuid4()) for _ in range(5)]
    cid = s.add_chunk("embed", "running", doc_ids=ids, item_count=5, attempts=MAX_CHUNK_ATTEMPTS)
    _run(s, cid)
    assert s.chunks[cid].status == "split"
    children = [c for c in s.of_kind("embed") if c.parent_id is not None]
    assert sorted(len(c.doc_ids) for c in children) == [2, 3]
    assert all(c.status == "pending" and str(c.parent_id) == cid for c in children)
    assert sorted(s.enqueued) == sorted(str(c.id) for c in children)
    assert sum((c.doc_ids for c in children), []) == ids


def test_single_doc_embed_failure_recorded_and_finalize_continues(env):
    s = env(status="finalizing")
    doc = str(uuid.uuid4())
    cid = s.add_chunk("embed", "running", doc_ids=[doc], item_count=1, attempts=MAX_CHUNK_ATTEMPTS)
    _run(s, cid)
    assert s.chunks[cid].status == "failed"
    assert any("search embeddings could not be generated" in e for e in s.job.errors)
    assert len(s.of_kind("post")) == 1  # completion is not blocked


def test_post_chunk_gives_up_and_completes_after_max_attempts(env):
    s = env(status="finalizing")
    cid = s.add_chunk("post", "running", item_count=0, attempts=MAX_CHUNK_ATTEMPTS)
    _run(s, cid)
    assert s.chunks[cid].status == "failed"
    assert s.job.status == "complete"
    assert any("threading" in e.lower() for e in s.job.errors)
    assert s.pipeline == []


def test_orphaned_pending_chunks_are_reenqueued(env):
    s = env()
    s.add_chunk("items", "done")
    orphan = s.add_chunk("items", "pending")
    s.orphans = [orphan]
    db = SimSession(s)
    asyncio.run(maybe_finalize(db, "j1"))
    assert s.enqueued == [orphan]
    assert s.job.status == "processing"
    sweep = next(q for q in db.executed if "SKIP LOCKED" in q)
    assert "35" in sweep or any(p and p.get("mins") == 35 for p in db.executed_params)


def test_no_sweep_while_a_chunk_is_running(env):
    s = env()
    s.add_chunk("items", "running")
    s.orphans = [s.add_chunk("items", "pending")]
    db = SimSession(s)
    asyncio.run(maybe_finalize(db, "j1"))
    assert s.enqueued == [] and not any("SKIP LOCKED" in q for q in db.executed)


def test_redelivered_finished_chunk_nudges_finalize(env, monkeypatch):
    s = env()
    cid = s.add_chunk("items", "done")
    seen = []

    async def fake_finalize(db, job_id):
        seen.append(job_id)

    monkeypatch.setattr(large_ingest, "maybe_finalize", fake_finalize)
    _run(s, cid)
    assert seen == ["j1"]


def test_finalize_ignores_jobs_without_large_containers(env):
    s = env()
    s.job.field_mapping = {}
    db = SimSession(s)
    asyncio.run(maybe_finalize(db, "j1"))
    assert not any("finalizing" in q for q in db.executed)


def test_finalize_job_if_done_delegates_for_large_jobs(monkeypatch):
    job = SimpleNamespace(id="j1", status="processing", source_format="native", total_files=1,
                          done_keys=[], skipped_keys=[], field_mapping={"large_containers": [PST]})
    seen = []

    async def fake_finalize(db, job_id):
        seen.append(job_id)

    async def boom(*a, **k):
        raise AssertionError("normal finalize must not run for a large job")

    monkeypatch.setattr(large_ingest, "maybe_finalize", fake_finalize)
    monkeypatch.setattr(ingest_mod, "generate_titles_batch", boom)
    monkeypatch.setattr(task_service, "enqueue_pipeline", lambda pid: (_ for _ in ()).throw(AssertionError()))
    db = FakeSession()
    asyncio.run(ingest_mod._finalize_job_if_done(db, job, 1, []))
    assert seen == ["j1"]
    assert job.status == "processing"


def test_native_batch_appends_errors_for_large_jobs(monkeypatch):
    """Normal batches in a large-container job must not overwrite errors that
    chunks (or the reconciliation line) appended concurrently."""
    small = {"storage_path": "productions/1/raw/a.txt", "relative_path": "a.txt",
             "filename": "a.txt", "size": 10}
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: [small])

    def boom(*a, **k):
        raise RuntimeError("bad file")

    monkeypatch.setattr(native_mod, "process_native_record", boom)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(ingest_mod, "_incr_skipped", noop)
    monkeypatch.setattr(ingest_mod, "_finalize_job_if_done", noop)

    class S(FakeSession):
        async def rollback(self):
            pass

    job = SimpleNamespace(id="j1", field_mapping={"large_containers": [PST]},
                          errors=["earlier chunk error"], source_format="native")
    db = S(get_objects={("IngestJob", "j1"): job,
                        ("Production", 1): SimpleNamespace(id=1, name="ACME Matter")})
    asyncio.run(native_mod.ingest_native_batch(db, "j1", 1, 0, 10))
    assert not any("SET errors = cast(:errs" in q for q in db.executed)
    appended = [json.loads(p["new"]) for q, p in zip(db.executed, db.executed_params)
                if "errors = errors ||" in q]
    assert len(appended) == 1 and len(appended[0]) == 1 and "bad file" in appended[0][0]


def test_process_chunk_route_dispatches_every_kind(monkeypatch):
    import app.routers.ingest as ri

    seen = []

    async def fake_run(db, cid):
        seen.append(cid)

    monkeypatch.setattr(large_ingest_chunk, "run_chunk", fake_run)
    for _kind in ("items", "embed", "post"):
        asyncio.run(ri.process_chunk_handler({"chunk_id": "c1"}, db=FakeSession(), _verified=None))
    assert seen == ["c1", "c1", "c1"]
