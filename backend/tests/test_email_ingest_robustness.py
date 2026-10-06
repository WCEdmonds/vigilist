"""Email-container ingest robustness (2026-10-06 Vote Thiru mbox incident).

Three failures from one night of mbox ingest:

1. ``will-part11.mbox``: one attachment's text produced a 1.5 MB tsvector
   (Postgres max 1 MB), so the family commit raised and all 201 messages in the
   container were dropped.
2. ``thiru-part01.mbox``: one message had a 722-char From header against a
   ``String(500)`` column; the whole 1,022-message container was dropped.
3. ``processed_files`` counts *documents* (messages + attachments) while
   ``total_files`` counts uploaded *containers*, so the first batch to finish
   saw ``2345 >= 13`` and marked the job complete while other batches ran.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from app.models import Document
from app.services import ingest as ingest_mod


# ── 1. tsvector input is capped ──────────────────────────────────────────


def _capture_db():
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    return db


def test_persist_documents_caps_tsvector_input():
    db = _capture_db()
    doc = Document(production_id=1, bates_begin="X 1", bates_end="X 1", text_content="a")
    doc.id = 42
    asyncio.run(ingest_mod._persist_documents(db, "job-1", [doc]))

    sqls = [str(c.args[0]) for c in db.execute.call_args_list]
    tsv = [s for s in sqls if "to_tsvector" in s]
    assert tsv, "expected a tsvector UPDATE"
    for s in tsv:
        assert f"left(COALESCE(text_content, ''), {ingest_mod.TSVECTOR_TEXT_CAP})" in s


def test_tsvector_cap_is_well_under_postgres_limit():
    # Postgres rejects a tsvector over 1,048,575 bytes. The cap bounds the
    # *input*; lexemes + positions stay far below the limit at this size.
    assert 0 < ingest_mod.TSVECTOR_TEXT_CAP <= 250_000


def test_tsvector_sql_expr_binds_cleanly():
    compiled = str(
        text(f"SELECT {ingest_mod.tsvector_sql(':txt')}").compile(
            dialect=postgresql.asyncpg.dialect()
        )
    )
    assert ":txt" not in compiled


# ── 2. bounded string columns are clamped, not fatal ─────────────────────


def test_clamp_string_columns_truncates_bounded_fields():
    doc = Document(
        production_id=1,
        bates_begin="X 1",
        bates_end="X 1",
        email_from="a" * 722,
        message_id="<" + "m" * 600 + ">",
        file_name="f" * 900,
        email_to="t" * 5000,  # Text column: unbounded, must be untouched
    )
    ingest_mod._clamp_string_columns(doc)
    assert len(doc.email_from) == 500
    assert len(doc.message_id) == 500
    assert len(doc.file_name) == 500
    assert len(doc.email_to) == 5000


def test_persist_documents_clamps_before_flush():
    db = _capture_db()
    doc = Document(production_id=1, bates_begin="X 1", bates_end="X 1", email_from="a" * 722)
    doc.id = 1
    asyncio.run(ingest_mod._persist_documents(db, "job-1", [doc]))
    assert len(doc.email_from) == 500


# ── 3. completion counts containers, not documents ───────────────────────


def _job(**kw):
    job = MagicMock()
    job.source_format = kw.get("source_format", "native")
    job.total_files = kw.get("total_files", 13)
    job.processed_files = kw.get("processed_files", 0)
    job.skipped_files = kw.get("skipped_files", 0)
    job.skipped_keys = kw.get("skipped_keys", [])
    job.done_keys = kw.get("done_keys", [])
    return job


def test_native_job_not_done_while_containers_outstanding():
    # 3 of 13 containers finished but they produced 2,345 documents.
    job = _job(processed_files=2344, skipped_files=1,
               done_keys=["native:a", "native:b"], skipped_keys=["native:c"])
    assert ingest_mod.sources_attempted(job) == 3
    assert not ingest_mod.job_is_done(job)


def test_native_job_done_when_every_container_accounted_for():
    keys = [f"native:{i}" for i in range(13)]
    job = _job(done_keys=keys[:12], skipped_keys=keys[12:])
    assert ingest_mod.job_is_done(job)


def test_native_retry_overlap_is_not_double_counted():
    # A retried batch re-sees a finished container and records it as skipped.
    job = _job(total_files=2, done_keys=["native:a"], skipped_keys=["native:a"])
    assert ingest_mod.sources_attempted(job) == 1
    assert not ingest_mod.job_is_done(job)


def test_non_native_jobs_keep_record_counting():
    job = _job(source_format="generic_pdf", total_files=5, processed_files=4, skipped_files=1)
    assert ingest_mod.job_is_done(job)


def test_mark_source_done_sql_is_guarded_and_binds_cleanly():
    sql = ingest_mod._MARK_SOURCE_DONE_SQL
    compiled = str(text(sql).compile(dialect=postgresql.asyncpg.dialect()))
    assert ":key" not in compiled and ":jid" not in compiled
    assert "NOT" in sql and "done_keys" in sql


def test_native_batch_marks_container_done_after_persist(monkeypatch):
    from app.services import ingest_native as native_mod

    job = MagicMock()
    job.field_mapping = {}
    job.errors = []
    job.name = "Vote Thiru"  # db.get serves this mock for the Production too
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    result = MagicMock()
    result.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    items = [{"storage_path": "p/a.mbox", "relative_path": "a.mbox", "filename": "a.mbox"}]
    monkeypatch.setattr(native_mod, "list_native_sources", lambda pid, lp=None: items)
    monkeypatch.setattr(native_mod, "process_native_email", lambda *a, **k: [MagicMock(), MagicMock()])
    done = []

    async def _spy_done(db, job_id, key):
        done.append(key)

    monkeypatch.setattr(ingest_mod, "_mark_source_done", _spy_done)
    monkeypatch.setattr(ingest_mod, "_persist_documents", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_persist_job_errors", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_finalize_job_if_done", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_stamp_source", lambda d, s: None)

    asyncio.run(native_mod.ingest_native_batch(db, "job-1", 9, 0, 10))
    assert done == ["native:p/a.mbox"]
