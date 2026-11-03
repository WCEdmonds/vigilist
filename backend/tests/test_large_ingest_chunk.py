"""Task 6: chunk worker. FakeSession + a fake pypff tree exploded into an
in-memory store (no GCS, no DB)."""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routers.ingest as ri
import app.services.ingest_native as native_mod
import app.services.tasks as task_service
from app.models import Document
from app.services import exploded_reader, large_ingest, pst_explode, pst_reader, storage
from app.services.ingest import append_job_errors, incr_job_items
from app.services import large_ingest_chunk
from app.services.large_ingest_chunk import MAX_CHUNK_ATTEMPTS, message_control, run_chunk
from app.services.oidc import verify_cloud_tasks_request
from tests.fakes import FakeResult, FakeSession
from tests.test_pst_reader import Folder, Item

PST = "productions/1/raw/acme.pst"
CONTROL = "ACME 000007"


class RollbackSession(FakeSession):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


def _tree(inbox_items, mixed_items=()):
    # Folder indices from the traversal: 0 "", 1 "Top", 2 "Top/Inbox", 3 "Top/Mixed".
    return Folder("", subs=[Folder("Top", subs=[
        Folder("Inbox", list(inbox_items)),
        Folder("Mixed", list(mixed_items)),
    ])])


def _emails(n, prefix="Msg"):
    return [Item(subject=f"{prefix} {i + 1}", plain_text_body=f"body {i + 1}") for i in range(n)]


class Env:
    def __init__(self, monkeypatch, tree, *, attempts=1, chunk=None, existing=(), total=None):
        self.opened = []  # every exploded object read
        self.enqueued = []
        self.finalized = []

        # The explode Job's output for this tree (job "j1", production 1).
        self.store = {}
        pst_explode.explode(tree, pst_explode.explode_prefix(1, "j1", PST),
                            lambda p, d, ct: self.store.__setitem__(p, bytes(d)), retry_sleep=0)

        def fake_get(path):
            self.opened.append(path)
            return self.store[path]

        monkeypatch.setattr(storage, "get_download_bytes", fake_get)
        monkeypatch.setattr(task_service, "enqueue_chunk", lambda cid: self.enqueued.append(cid))
        monkeypatch.setattr(native_mod, "_store_attachment", lambda data, path: None)

        async def fake_finalize(db, job_id):
            self.finalized.append(job_id)

        monkeypatch.setattr(large_ingest, "maybe_finalize", fake_finalize)

        self.chunk = chunk
        survey = pst_reader.survey(tree)
        survey.storage_path = PST
        self.job = SimpleNamespace(
            id="j1", production_id=1, status="processing",
            field_mapping={"custodian": "Alice Example", "source_party": "ACME", "source_type": "collection"},
            survey={PST: survey.to_json()},
            selection={PST: {"types": ["email"], "include_recoverable": True, "control": CONTROL,
                             "relative_path": "acme.pst", "items": total or chunk.item_count}},
        )
        self.db = RollbackSession(
            get_objects={("IngestChunk", "c1"): chunk, ("IngestJob", "j1"): self.job},
            responders=[
                ("RETURNING ingest_chunks.attempts", FakeResult(scalar=attempts)),
                ("RETURNING attempts", FakeResult(scalar=attempts)),
                ("documents.bates_begin IN", FakeResult(rows=[(c,) for c in existing])),
                ("status = 'split'", FakeResult(rowcount=1)),
                ("status = 'failed'", FakeResult(rowcount=1)),
                ("status = 'done'", FakeResult(rowcount=1)),
            ],
        )

    def run(self):
        asyncio.run(run_chunk(self.db, "c1"))

    def docs(self):
        return [o for o in self.db.added if isinstance(o, Document)]

    def parents(self):
        return [d for d in self.docs() if d.metadata_.get("Parent") is None]

    def sql_params(self, needle):
        return [p for s, p in zip(self.db.executed, self.db.executed_params) if needle in s]

    def errors_appended(self):
        return [m for p in self.sql_params("errors ||") for m in json.loads(p["new"])]


def _chunk(slices, item_count, *, global_offset=0, type_filter=(), attempts=0):
    return SimpleNamespace(
        id="c1", job_id="j1", kind="items", container_path=PST, slices=slices,
        type_filter=list(type_filter), global_offset=global_offset, item_count=item_count,
        status="running", attempts=attempts, parent_id=None,
    )


def test_message_control_width_grows_with_total():
    assert message_control("ACME 000007", 12, 9_999) == "ACME 000007 -0012"
    assert message_control("ACME 000007", 12, 75_000) == "ACME 000007 -00012"


def test_max_chunk_attempts():
    assert MAX_CHUNK_ATTEMPTS == 3


def test_chunk_builds_documents_with_folder_and_controls(monkeypatch):
    # 3 emails in slice -> 3 parent Documents, bates "ACME 000007 -0001..0003",
    # metadata_["PST Folder"] == "Top/Inbox"
    env = Env(monkeypatch, _tree(_emails(3)), chunk=_chunk([[2, 0, 3]], 3))
    env.run()
    parents = env.parents()
    assert [d.bates_begin for d in parents] == [f"{CONTROL} -000{i}" for i in (1, 2, 3)]
    assert all(d.metadata_["PST Folder"] == "Top/Inbox" for d in parents)
    assert [d.title for d in parents] == ["Msg 1", "Msg 2", "Msg 3"]
    assert all(d.native_path is None for d in parents)  # no container-level dedup key
    assert all(d.source_party == "ACME" and d.custodian == "Alice Example" for d in parents)
    assert all(d.source_path == "acme.pst" for d in parents)
    assert sum(p["d"] for p in env.sql_params("items_done = items_done +")) == 3
    assert env.sql_params("status = 'done'")
    assert env.finalized == ["j1"]


def test_chunk_numbering_uses_global_offset(monkeypatch):
    env = Env(monkeypatch, _tree(_emails(6)), chunk=_chunk([[2, 3, 6]], 3, global_offset=3), total=6)
    env.run()
    assert [d.bates_begin for d in env.parents()] == [f"{CONTROL} -000{i}" for i in (4, 5, 6)]
    assert [d.title for d in env.parents()] == ["Msg 4", "Msg 5", "Msg 6"]


def test_attachments_share_folder_and_family(monkeypatch):
    from tests.test_pst_reader import Att

    item = Item(subject="With file", plain_text_body="see", attachments=[Att("notes.txt", b"hello")])
    env = Env(monkeypatch, _tree([item]), chunk=_chunk([[2, 0, 1]], 1))
    env.run()
    docs = env.docs()
    assert [d.bates_begin for d in docs] == [f"{CONTROL} -0001", f"{CONTROL} -0001 .0001"]
    assert all(d.metadata_["PST Folder"] == "Top/Inbox" for d in docs)
    assert {d.family_id for d in docs} == {f"{CONTROL} -0001"}


def test_retry_skips_existing_controls_and_counts_once(monkeypatch):
    # controls -0001,-0002 already exist -> only -0003 persisted; items_done += 1 once
    env = Env(monkeypatch, _tree(_emails(3)), chunk=_chunk([[2, 0, 3]], 3), attempts=2,
              existing=[f"{CONTROL} -0001", f"{CONTROL} -0002"])
    env.run()
    assert [d.bates_begin for d in env.parents()] == [f"{CONTROL} -0003"]
    done = env.sql_params("items_done = items_done +")
    assert [p["d"] for p in done] == [1]
    # The existing-control lookup is one query over the chunk's controls.
    assert len(env.sql_params("documents.bates_begin IN")) == 1


def test_items_done_rides_in_the_family_commit(monkeypatch):
    """A crash right after a family commits must not lose its items_done:
    the counter is in the same UPDATE as processed_files."""
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1))
    env.run()
    (sql,) = [s for s in env.db.executed if "items_done = items_done +" in s]
    assert "processed_files = processed_files +" in sql


def test_fourth_attempt_splits_chunk(monkeypatch):
    # attempts already 3, item_count 10 -> two children 5/5, parent "split", 2 enqueues
    env = Env(monkeypatch, _tree(_emails(10)), chunk=_chunk([[2, 0, 10]], 10, attempts=3),
              attempts=MAX_CHUNK_ATTEMPTS + 1)
    env.run()
    children = [o for o in env.db.added if type(o).__name__ == "IngestChunk"]
    assert [c.item_count for c in children] == [5, 5]
    assert [c.global_offset for c in children] == [0, 5]
    assert [c.slices for c in children] == [[[2, 0, 5]], [[2, 5, 10]]]
    assert all(c.parent_id == "c1" and c.kind == "items" and c.status == "pending" for c in children)
    assert all(c.container_path == PST for c in children)
    assert env.sql_params("status = 'split'")
    assert env.enqueued == [str(c.id) for c in children]
    assert env.opened == [] and env.docs() == []
    assert env.finalized == []


def test_split_of_filtered_chunk_halves_selected_items(monkeypatch):
    mixed = [Item(subject="e1"), Item("IPM.Contact"), Item(subject="e2"), Item(subject="e3"),
             Item("IPM.Contact"), Item(subject="e4")]
    env = Env(monkeypatch, _tree([], mixed), chunk=_chunk([[3, 0, 6]], 4, global_offset=10,
                                                          type_filter=["email"], attempts=3),
              attempts=MAX_CHUNK_ATTEMPTS + 1, total=20)
    env.run()
    children = [o for o in env.db.added if type(o).__name__ == "IngestChunk"]
    assert [(c.slices, c.item_count, c.global_offset) for c in children] == [
        ([[3, 0, 3]], 2, 10), ([[3, 3, 6]], 2, 12),
    ]
    assert [c.type_filter for c in children] == [["email"], ["email"]]


def test_single_item_chunk_over_attempts_is_recorded_failed(monkeypatch):
    # item_count 1, attempts 3 -> error with folder/position text, items_failed += 1, status "failed"
    env = Env(monkeypatch, _tree(_emails(3, prefix="Poison")), chunk=_chunk([[2, 1, 2]], 1, global_offset=1, attempts=3),
              attempts=MAX_CHUNK_ATTEMPTS + 1, total=3)
    env.run()
    (msg,) = env.errors_appended()
    assert msg.startswith(f"{CONTROL} -0002")
    assert "Top/Inbox" in msg and "position 1" in msg and "Poison 2" in msg
    assert [p["f"] for p in env.sql_params("items_failed = items_failed +")] == [1]
    assert env.sql_params("status = 'failed'")
    assert env.docs() == []
    assert env.finalized == ["j1"]


def test_single_item_failure_skips_subject_read_after_first_try(monkeypatch):
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1, attempts=4),
              attempts=MAX_CHUNK_ATTEMPTS + 2)
    env.run()
    assert env.opened == []  # reading it may be what crashed the worker
    (msg,) = env.errors_appended()
    assert "Top/Inbox" in msg and "position 0" in msg


def test_one_bad_item_does_not_fail_chunk(monkeypatch):
    # to_parsed raises for item 2 of 3 -> items 1 and 3 persisted, one error appended, chunk "done"
    real = exploded_reader.to_parsed

    def flaky(item, kind):
        if getattr(item, "subject", "") == "Msg 2":
            raise RuntimeError("corrupt body")
        return real(item, kind)

    monkeypatch.setattr(exploded_reader, "to_parsed", flaky)
    env = Env(monkeypatch, _tree(_emails(3)), chunk=_chunk([[2, 0, 3]], 3))
    env.run()
    assert [d.bates_begin for d in env.parents()] == [f"{CONTROL} -0001", f"{CONTROL} -0003"]
    (msg,) = env.errors_appended()
    assert msg.startswith(f"{CONTROL} -0002") and "corrupt body" in msg and "Top/Inbox" in msg
    assert [p["f"] for p in env.sql_params("items_failed = items_failed +")] == [1]
    assert env.sql_params("status = 'done'")


def test_unselected_types_in_folder_are_skipped(monkeypatch):
    # chunk type_filter=["email"]; folder holds email, contact, email -> 2 Documents, positions contiguous
    mixed = [Item(subject="first"), Item("IPM.Contact", subject="a contact"), Item(subject="second")]
    env = Env(monkeypatch, _tree([], mixed), chunk=_chunk([[3, 0, 3]], 2, type_filter=["email"]))
    env.run()
    parents = env.parents()
    assert [d.bates_begin for d in parents] == [f"{CONTROL} -0001", f"{CONTROL} -0002"]
    assert [d.title for d in parents] == ["first", "second"]
    assert all(d.metadata_["PST Folder"] == "Top/Mixed" for d in parents)


def test_non_email_item_is_loaded_with_its_type(monkeypatch):
    cal = [Item("IPM.Appointment", subject="Kickoff", plain_text_body="agenda")]
    env = Env(monkeypatch, _tree(cal), chunk=_chunk([[2, 0, 1]], 1))
    env.run()
    (doc,) = env.parents()
    assert doc.title == "Kickoff" and doc.metadata_["PST Item Type"] == "calendar"


def test_short_slice_records_missing_items_as_failed(monkeypatch):
    # The chunk expects 3 selected items but the slice holds only 2 of them.
    mixed = [Item(subject="first"), Item("IPM.Contact"), Item(subject="second")]
    env = Env(monkeypatch, _tree([], mixed), chunk=_chunk([[3, 0, 3]], 3, type_filter=["email"]))
    env.run()
    assert len(env.parents()) == 2
    assert [p["f"] for p in env.sql_params("items_failed = items_failed +")] == [1]
    assert any("expected 3" in m for m in env.errors_appended())


def test_finished_chunk_returns_immediately(monkeypatch):
    # A redelivered task for a finished chunk does no work, but still nudges
    # finalize (a crash after the done commit may have skipped it).
    chunk = _chunk([[2, 0, 1]], 1)
    chunk.status = "done"
    env = Env(monkeypatch, _tree(_emails(1)), chunk=chunk, attempts=None)
    env.run()
    assert env.opened == [] and env.docs() == [] and env.finalized == ["j1"]


def test_chunk_held_by_live_attempt_is_busy(monkeypatch):
    # The claim refused a fresh 'running' chunk: another attempt holds it.
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1), attempts=None)
    with pytest.raises(large_ingest_chunk.ChunkBusy):
        env.run()
    assert env.opened == [] and env.docs() == [] and env.finalized == []


def test_claim_passes_stale_window(monkeypatch):
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1))
    env.run()
    claim = env.sql_params("RETURNING attempts")
    assert claim and claim[0]["stale"] == large_ingest_chunk.CHUNK_STALE_S


def test_whole_chunk_error_releases_claim(monkeypatch):
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1))

    def broken_get(path):
        raise OSError("storage unavailable")

    monkeypatch.setattr(storage, "get_download_bytes", broken_get)
    with pytest.raises(OSError):
        env.run()
    assert env.sql_params("SET status = 'pending'")


def test_claim_is_one_conditional_statement(monkeypatch):
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1))
    env.run()
    claim = env.db.executed[0]
    assert "attempts = attempts + 1" in claim and "'running'" in claim
    assert "'pending'" in claim and "RETURNING" in claim


def test_append_job_errors_is_one_atomic_update():
    db = FakeSession()
    asyncio.run(append_job_errors(db, "j1", ["a", "b"]))
    (sql,) = db.executed
    assert sql.startswith("UPDATE ingest_jobs") and "errors = errors || cast(:new as jsonb)" in sql
    assert json.loads(db.executed_params[0]["new"]) == ["a", "b"]
    assert db.commits == 1


def test_incr_job_items_is_atomic():
    db = FakeSession()
    asyncio.run(incr_job_items(db, "j1", done=2, failed=1))
    (sql,) = db.executed
    assert "items_done = items_done + :d" in sql and "items_failed = items_failed + :f" in sql
    assert db.executed_params[0] == {"d": 2, "f": 1, "jid": "j1"}


def test_process_chunk_endpoint_requires_cloud_tasks_oidc():
    sig = inspect.signature(ri.process_chunk_handler)
    assert any(p.default.dependency is verify_cloud_tasks_request
               for p in sig.parameters.values() if hasattr(p.default, "dependency"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(ri.process_chunk_handler(body={}, db=FakeSession()))
    assert e.value.status_code == 400


def test_process_chunk_endpoint_runs_chunk(monkeypatch):
    calls = []

    async def fake_run(db, chunk_id):
        calls.append(chunk_id)

    monkeypatch.setattr(large_ingest_chunk, "run_chunk", fake_run)
    assert asyncio.run(ri.process_chunk_handler(body={"chunk_id": "c9"}, db=FakeSession())) == {"ok": True}
    assert calls == ["c9"]


def _raise_integrity(monkeypatch):
    from sqlalchemy.exc import IntegrityError

    import app.services.ingest as ingest_mod

    async def boom(db, job_id, docs, **kw):
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    monkeypatch.setattr(ingest_mod, "_persist_documents", boom)


def test_concurrent_duplicate_is_skipped_not_failed(monkeypatch):
    _raise_integrity(monkeypatch)
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1))
    env.db.responders.insert(0, ("documents.bates_begin = ", FakeResult(items=[(42,)])))
    env.run()
    assert env.errors_appended() == []
    assert env.sql_params("items_failed = items_failed +") == []
    assert env.sql_params("status = 'done'")


def test_integrity_error_without_a_duplicate_is_recorded_failed(monkeypatch):
    _raise_integrity(monkeypatch)
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1))
    env.run()
    (msg,) = env.errors_appended()
    assert msg.startswith(f"{CONTROL} -0001") and "duplicate key" in msg
    assert [p["f"] for p in env.sql_params("items_failed = items_failed +")] == [1]


def _crash_then_retry(monkeypatch, fail_on_retry):
    """Attempt 1: item 1 fails, then the worker dies at item 3 (as an OOM
    would). Attempt 2 re-runs the chunk; item 2 is already loaded."""
    real_parse = exploded_reader.to_parsed
    real_iter = exploded_reader.iter_slice
    state = {"attempt": 1}

    def flaky(item, kind):
        if item.subject == "Msg 1" and (state["attempt"] == 1 or fail_on_retry):
            raise RuntimeError("bad body")
        return real_parse(item, kind)

    def crashing_iter(job_id, production_id, storage_path, slices):
        for n, row in enumerate(real_iter(job_id, production_id, storage_path, slices)):
            if state["attempt"] == 1 and n == 2:
                raise MemoryError("worker died")
            yield row

    monkeypatch.setattr(exploded_reader, "to_parsed", flaky)
    monkeypatch.setattr(exploded_reader, "iter_slice", crashing_iter)
    env = Env(monkeypatch, _tree(_emails(3)), chunk=_chunk([[2, 0, 3]], 3))
    with pytest.raises(MemoryError):
        env.run()
    # Nothing failed was recorded by the crashed attempt.
    assert env.errors_appended() == [] and env.sql_params("items_failed = items_failed +") == []
    state["attempt"] = 2
    loaded = [d.bates_begin for d in env.parents()]
    assert loaded == [f"{CONTROL} -0002"]
    env.db.responders.insert(0, ("documents.bates_begin IN", FakeResult(rows=[(c,) for c in loaded])))
    env.run()
    return env


def test_failure_then_crash_then_failure_again_is_recorded_once(monkeypatch):
    env = _crash_then_retry(monkeypatch, fail_on_retry=True)
    failed = [p["f"] for p in env.sql_params("items_failed = items_failed +")]
    assert sum(failed) == 1
    (msg,) = env.errors_appended()
    assert msg.startswith(f"{CONTROL} -0001")
    assert [d.bates_begin for d in env.parents()] == [f"{CONTROL} -0002", f"{CONTROL} -0003"]


def test_transient_failure_loaded_on_retry_is_not_recorded(monkeypatch):
    env = _crash_then_retry(monkeypatch, fail_on_retry=False)
    assert env.sql_params("items_failed = items_failed +") == []
    assert env.errors_appended() == []
    assert sorted(d.bates_begin for d in env.parents()) == [f"{CONTROL} -000{i}" for i in (1, 2, 3)]


def test_failures_not_recorded_when_done_transition_loses(monkeypatch):
    """A concurrent dispatch already finished the chunk: its failures are its own."""
    monkeypatch.setattr(exploded_reader, "to_parsed", lambda item, kind: (_ for _ in ()).throw(RuntimeError("x")))
    env = Env(monkeypatch, _tree(_emails(1)), chunk=_chunk([[2, 0, 1]], 1))
    env.db.responders.insert(0, ("status = 'done'", FakeResult(rowcount=0)))
    env.run()
    assert env.errors_appended() == [] and env.sql_params("items_failed = items_failed +") == []


# --- C1: pre-claim failures are capped --------------------------------------


class _DownSession(RollbackSession):
    """The claim UPDATE raises, as with the database unavailable."""

    async def execute(self, stmt, params=None):
        if "RETURNING attempts" in str(stmt):
            raise OSError("connection refused")
        return await super().execute(stmt, params)


def _down_db():
    chunk = SimpleNamespace(id="c1", job_id="j1")
    return _DownSession(get_objects={("IngestChunk", "c1"): chunk})


def test_claim_failure_is_a_claim_error():
    with pytest.raises(large_ingest_chunk.ChunkClaimError):
        asyncio.run(run_chunk(_down_db(), "c1"))


def test_process_chunk_claim_failure_retries_below_the_cap():
    for retry in ("0", "1"):
        with pytest.raises(HTTPException) as e:
            asyncio.run(ri.process_chunk_handler(body={"chunk_id": "c1"}, db=_down_db(),
                                                 retry_count=retry))
        assert e.value.status_code == 500


def test_process_chunk_claim_failure_is_dropped_past_the_cap():
    db = _down_db()
    out = asyncio.run(ri.process_chunk_handler(body={"chunk_id": "c1"}, db=db, retry_count="2"))
    assert out == {"ok": False, "dropped": True}
    (new,) = [json.loads(p["new"]) for s, p in zip(db.executed, db.executed_params) if "errors ||" in s]
    assert new[0].startswith("Chunk c1: could not be started after 3 attempts")
    assert "connection refused" in new[0]


def test_poll_batch_pre_claim_failure_is_capped(monkeypatch):
    from app.services import pipeline

    async def broken(production_id, stage):
        raise pipeline.BatchPollClaimError("db down")

    monkeypatch.setattr(pipeline, "poll_batch_stage", broken)
    body = {"production_id": 1, "stage": "summaries"}
    with pytest.raises(HTTPException) as e:
        asyncio.run(ri.poll_batch_handler(body, None, retry_count="1"))
    assert e.value.status_code == 500
    assert asyncio.run(ri.poll_batch_handler(body, None, retry_count="2")) == {
        "ok": False, "dropped": True}


def test_poll_batch_stage_wraps_failures_before_the_claim(monkeypatch):
    from app.services import pipeline

    class Broken:
        async def __aenter__(self):
            raise OSError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(pipeline, "async_session", lambda: Broken())
    with pytest.raises(pipeline.BatchPollClaimError):
        asyncio.run(pipeline.poll_batch_stage(1, "summaries"))


# --- I4: a crash while building one item skips that item on the next claim ---


class WorkerDied(BaseException):
    """Stands in for an OOM kill / instance death: no except Exception runs."""


def _always_dies_on(monkeypatch, subject):
    real = exploded_reader.to_parsed
    built = []

    def dying(item, kind):
        built.append(item.subject)
        if item.subject == subject:
            raise WorkerDied()
        return real(item, kind)

    monkeypatch.setattr(exploded_reader, "to_parsed", dying)
    return built


def test_item_position_is_recorded_before_building_it(monkeypatch):
    env = Env(monkeypatch, _tree(_emails(3)), chunk=_chunk([[2, 0, 3]], 3))
    env.run()
    assert [p["pos"] for p in env.sql_params("SET cursor = :pos")] == [0, 1, 2]


def test_crash_while_building_an_item_skips_it_on_the_next_claim(monkeypatch):
    built = _always_dies_on(monkeypatch, "Msg 2")
    chunk = _chunk([[2, 0, 3]], 3)
    chunk.cursor = None
    env = Env(monkeypatch, _tree(_emails(3)), chunk=chunk)
    with pytest.raises(WorkerDied):
        env.run()
    assert env.sql_params("SET status = 'pending'") == []  # a dead worker can't release
    cursor = env.sql_params("SET cursor = :pos")[-1]["pos"]
    assert cursor == 1
    # Re-claim: the row still carries the crashed attempt's position.
    chunk.cursor = cursor
    loaded = [d.bates_begin for d in env.parents()]
    assert loaded == [f"{CONTROL} -0001"]
    env.db.responders.insert(0, ("documents.bates_begin IN", FakeResult(rows=[(c,) for c in loaded])))
    built.clear()
    env.run()
    assert built == ["Msg 3"]  # the poison item is not built again
    assert [d.bates_begin for d in env.parents()] == [f"{CONTROL} -0001", f"{CONTROL} -0003"]
    (msg,) = env.errors_appended()
    assert msg.startswith(f"{CONTROL} -0002") and "Top/Inbox" in msg and "position 1" in msg
    assert "stopped while loading it" in msg
    assert [p["f"] for p in env.sql_params("items_failed = items_failed +")] == [1]
    assert env.sql_params("status = 'done'")


def test_recorded_position_of_a_loaded_item_is_not_skipped(monkeypatch):
    """The crash came after that item committed (e.g. while reading the next
    one): nothing is skipped."""
    chunk = _chunk([[2, 0, 3]], 3)
    chunk.cursor = 0
    env = Env(monkeypatch, _tree(_emails(3)), chunk=chunk, attempts=2,
              existing=[f"{CONTROL} -0001"])
    env.run()
    assert [d.bates_begin for d in env.parents()] == [f"{CONTROL} -0002", f"{CONTROL} -0003"]
    assert env.errors_appended() == []


def test_release_after_an_error_clears_the_position(monkeypatch):
    """A raised error is not a crash: the retry must not skip anything."""
    assert "cursor = NULL" in large_ingest_chunk._RELEASE_CHUNK_SQL


def test_chunk_model_has_a_cursor_column():
    from app.models import IngestChunk

    assert IngestChunk.__table__.c.cursor.nullable


# --- I4: attachment size cap -------------------------------------------------


def test_oversized_attachment_is_not_read(monkeypatch):
    from tests.test_pst_reader import Att

    monkeypatch.setattr(pst_reader, "MAX_ATTACHMENT_BYTES", 10)
    big = Att("huge.zip", b"x" * 11)
    big.read_buffer = lambda n: pytest.fail("oversized attachment was read")
    item = Item(subject="With huge file", plain_text_body="see",
                attachments=[Att("small.txt", b"hello"), big])
    env = Env(monkeypatch, _tree([item]), chunk=_chunk([[2, 0, 1]], 1))
    env.run()
    docs = env.docs()
    assert [d.bates_begin for d in docs] == [
        f"{CONTROL} -0001", f"{CONTROL} -0001 .0001", f"{CONTROL} -0001 .0002"]
    huge = docs[2]
    assert huge.file_name == "huge.zip" and huge.native_path is None
    assert huge.extraction_status == "error" and "too large" in huge.extraction_error
    assert huge.text_content is None and huge.family_id == f"{CONTROL} -0001"
    assert docs[1].extraction_status == "ok"


def test_max_attachment_bytes_is_512_mib():
    assert pst_reader.MAX_ATTACHMENT_BYTES == 512 * 1024 * 1024


def test_resumed_container_loads_only_messages_the_earlier_job_missed(monkeypatch):
    """I5: with the earlier job's container control reused, its loaded
    messages are skipped and only the rest are built."""
    env = Env(monkeypatch, _tree(_emails(4)), chunk=_chunk([[2, 0, 4]], 4),
              existing=[f"{CONTROL} -0001", f"{CONTROL} -0002"])
    env.run()
    assert [d.bates_begin for d in env.parents()] == [f"{CONTROL} -0003", f"{CONTROL} -0004"]
    assert [d.title for d in env.parents()] == ["Msg 3", "Msg 4"]


def test_single_item_over_attempts_already_loaded_is_done_not_failed(monkeypatch):
    """m1: a concurrent attempt loaded the item: no failure is recorded."""
    env = Env(monkeypatch, _tree(_emails(3)), chunk=_chunk([[2, 1, 2]], 1, global_offset=1, attempts=3),
              attempts=MAX_CHUNK_ATTEMPTS + 1, total=3)
    env.db.responders.insert(0, ("documents.bates_begin = ", FakeResult(items=[(42,)])))
    env.run()
    assert env.errors_appended() == []
    assert env.sql_params("items_failed = items_failed +") == []
    assert env.sql_params("status = 'done'") and not env.sql_params("status = 'failed'")
    assert env.opened == []  # no subject read
    assert env.finalized == ["j1"]


# --- Unit 8: chunks read the exploded objects --------------------------------


def test_chunk_over_exploded_objects_matches_building_from_the_pst(monkeypatch):
    """Same documents and control numbers as building straight from pypff."""
    from tests.test_pst_reader import Att

    items = [Item(subject="One", plain_text_body="b1", attachments=[Att("a.txt", b"hello")]),
             Item("IPM.Appointment", subject="Two", sender_name="Bob"),
             Item(subject="Three", html_body="<p>hi</p>")]
    env = Env(monkeypatch, _tree(items), chunk=_chunk([[2, 0, 3]], 3))
    env.run()
    expected = []
    for n, it in enumerate(items, start=1):
        kind = pst_reader.item_kind(it)
        parsed = pst_reader.to_parsed(it, kind)
        expected += native_mod.build_email_documents(
            parsed, message_control=message_control(CONTROL, n, 3), production_id=1,
            source_path="acme.pst", custodian="Alice Example",
            msg_bytes=native_mod._serialize_message(parsed), store_fn=lambda d, p: None)

    def key(d):
        return (d.bates_begin, d.family_id, d.title, d.text_content, d.file_hash_sha256,
                d.email_subject, d.file_name)

    assert [key(d) for d in env.docs()] == [key(d) for d in expected]


def test_chunk_never_opens_or_downloads_the_pst(monkeypatch):
    env = Env(monkeypatch, _tree(_emails(2)), chunk=_chunk([[2, 0, 2]], 2))
    monkeypatch.setattr(pst_reader, "open_pst", lambda *a, **k: pytest.fail("PST opened"))
    monkeypatch.setattr(storage, "download_sliced", lambda *a, **k: pytest.fail("PST downloaded"))
    env.run()
    assert len(env.parents()) == 2
    assert all("/exploded/j1/" in p for p in env.opened)


def test_partly_read_item_is_loaded_and_reported_not_failed(monkeypatch):
    from tests.test_pst_reader import _item_with

    cls = _item_with(subject=OSError("unable to read subject"))
    env = Env(monkeypatch, _tree([cls(plain_text_body="kept")]), chunk=_chunk([[2, 0, 1]], 1))
    env.run()
    (doc,) = env.parents()
    assert doc.text_content == "kept" and "unable to read subject" in doc.metadata_["Read Errors"]
    (msg,) = env.errors_appended()
    assert msg.startswith(f"{CONTROL} -0001: partly read") and "position 0" in msg
    assert env.sql_params("items_failed = items_failed +") == []


def test_item_unreadable_at_unpack_is_recorded_failed_with_its_position(monkeypatch):
    real = pst_reader.to_parsed

    def broken(item, kind, _depth=0):
        if getattr(item, "subject", "") == "Msg 2":
            raise OSError("item table corrupt")
        return real(item, kind, _depth)

    monkeypatch.setattr(pst_reader, "to_parsed", broken)  # at explode time
    env = Env(monkeypatch, _tree(_emails(3)), chunk=_chunk([[2, 0, 3]], 3))
    monkeypatch.setattr(pst_reader, "to_parsed", real)
    env.run()
    assert [d.bates_begin for d in env.parents()] == [f"{CONTROL} -0001", f"{CONTROL} -0003"]
    (msg,) = env.errors_appended()
    assert msg.startswith(f"{CONTROL} -0002") and "Top/Inbox" in msg and "position 1" in msg
    assert "item table corrupt" in msg
    assert [p["f"] for p in env.sql_params("items_failed = items_failed +")] == [1]
