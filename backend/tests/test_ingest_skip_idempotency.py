"""Skip-counter idempotency across Cloud Tasks retries.

Regression for the 2026-07-24 prod incident: an OOM-killed batch was retried
every ~10s by Cloud Tasks; each retry re-walked its slice and re-counted every
already-persisted document as "skipped", so the job showed 44/73 skipped while
still crash-looping. Inflated counters can also fire the completion check
``(processed + skipped) >= total`` before every record was actually attempted.

The fix keys every skip event to a stable per-record identity and makes the
increment a single guarded UPDATE (no-op when the key was already counted).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from app.services import ingest as ingest_mod


def test_incr_skipped_sql_is_guarded_and_binds_cleanly():
    """The skip UPDATE must be atomic and idempotent per (job, key).

    - compiles under the asyncpg dialect with no literal ``:param`` surviving
      (see test_job_errors_update_sql_binds_both_params for the failure mode)
    - carries a containment guard so a re-counted key cannot increment again
    """
    sql = ingest_mod._INCR_SKIPPED_SQL

    compiled = str(text(sql).compile(dialect=postgresql.asyncpg.dialect()))
    assert ":key" not in compiled
    assert ":jid" not in compiled

    assert "skipped_files + 1" in sql
    # Guard: only increment when the key is not already recorded.
    assert "NOT" in sql and "skipped_keys" in sql


def _spy_incr_skipped(calls):
    async def _spy(db, job_id, key):
        calls.append(key)

    return _spy


def _fake_db(job, existing_rows):
    """AsyncSession stand-in: returns ``job`` from get, ``existing_rows``
    from the existing-Bates/paths select, and swallows everything else."""
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    result = MagicMock()
    result.all.return_value = [(r,) for r in existing_rows]
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _run_ingest_batch_once(monkeypatch, calls, records, existing):
    job = MagicMock()
    job.field_mapping = {}
    job.errors = []

    monkeypatch.setattr(
        ingest_mod, "bootstrap_ingest_source", lambda pid, load_prefix=None: (records, {})
    )
    # Record C fails to build every time (the "poison document").
    monkeypatch.setattr(
        ingest_mod,
        "process_ingest_record",
        lambda pid, record, opt_pages, tmp, errors, fm: None,
    )
    monkeypatch.setattr(ingest_mod, "_incr_skipped", _spy_incr_skipped(calls))
    monkeypatch.setattr(ingest_mod, "_persist_document", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_persist_job_errors", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_finalize_job_if_done", AsyncMock())

    db = _fake_db(job, existing)
    asyncio.run(ingest_mod.ingest_batch(db, "job-1", 7, 0, len(records)))


def test_ingest_batch_skip_keys_stable_across_retries(monkeypatch):
    """A retried slice must produce the SAME key per skipped record.

    Slice of three: a row with no Bates, a record already ingested by an
    earlier pass, and a record that fails to build. Two runs simulate the
    Cloud Tasks retry; identical keys mean the guarded UPDATE counts each
    record once no matter how many times the batch is retried.
    """
    records = [
        {"Begin Bates": ""},            # malformed row → positional key
        {"Begin Bates": "ACME 000001"}, # already persisted → skipped
        {"Begin Bates": "ACME 000002"}, # poison record → skipped via None
    ]

    first_run: list[str] = []
    _run_ingest_batch_once(monkeypatch, first_run, records, existing=["ACME 000001"])

    retry_run: list[str] = []
    _run_ingest_batch_once(monkeypatch, retry_run, records, existing=["ACME 000001"])

    assert first_run == retry_run
    assert len(first_run) == len(set(first_run)) == 3
    # Positional key for the Bates-less row must encode the global index,
    # not anything run-dependent.
    assert first_run[0] == retry_run[0]
    assert "ACME 000001" in first_run[1]
    assert "ACME 000002" in first_run[2]


def test_pdf_batch_skip_keys_use_storage_path(monkeypatch):
    """The generic-PDF path keys skips on the stable storage path."""
    from app.services import ingest_pdf as pdf_mod

    items = [
        {"storage_path": "productions/7/raw/A/dup.pdf", "relative_path": "A/dup.pdf",
         "filename": "dup.pdf"},
        {"storage_path": "productions/7/raw/B/poison.pdf", "relative_path": "B/poison.pdf",
         "filename": "poison.pdf"},
    ]
    # ingest_pdf_batch imports these inside the function body, straight from
    # app.services.ingest_pdf — patch the source module, not ingest.
    monkeypatch.setattr(pdf_mod, "list_pdf_sources", lambda pid, load_prefix=None: items)
    monkeypatch.setattr(pdf_mod, "process_pdf_record", lambda *a, **k: None)

    calls: list[str] = []
    monkeypatch.setattr(ingest_mod, "_incr_skipped", _spy_incr_skipped(calls))
    monkeypatch.setattr(ingest_mod, "_persist_document", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_persist_job_errors", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_finalize_job_if_done", AsyncMock())

    job = MagicMock()
    job.field_mapping = {"control_offset": 0}
    job.errors = []
    production = MagicMock()
    production.name = "Acme Matter"
    db = _fake_db(job, existing_rows=["productions/7/raw/A/dup.pdf"])
    db.get = AsyncMock(side_effect=[job, production])

    asyncio.run(ingest_mod.ingest_pdf_batch(db, "job-1", 7, 0, 2))

    assert len(calls) == 2
    assert "productions/7/raw/A/dup.pdf" in calls[0]
    assert "productions/7/raw/B/poison.pdf" in calls[1]
