"""One failed record must not poison the rest of its batch.

Prod incident follow-up (2026-07-25): after any record failed, the handler's
``await db.rollback()`` expired every ORM object in the session — including
the ``job`` row. The next record's ``_stamp_source(doc, job)`` then touched
``job.field_mapping``, triggering a synchronous lazy refresh that is illegal
on an async session (``greenlet_spawn has not been called``), which the same
handler caught and rolled back — cascading the failure to every remaining
record in the slice. Ten documents were lost this way on a healthy container.

The fix snapshots the job's stamp fields into a plain dict once per batch,
before the loop, so no ORM attribute is read after a rollback.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from app.services import ingest as ingest_mod


def test_records_after_a_failure_still_persist(monkeypatch):
    from app.services import ingest_pdf as pdf_mod

    items = [
        {"storage_path": "productions/7/raw/A/poison.pdf", "relative_path": "A/poison.pdf",
         "filename": "poison.pdf"},
        {"storage_path": "productions/7/raw/B/fine.pdf", "relative_path": "B/fine.pdf",
         "filename": "fine.pdf"},
    ]
    monkeypatch.setattr(pdf_mod, "list_pdf_sources", lambda pid, load_prefix=None: items)

    calls = {"n": 0}

    def flaky_process(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # the poison record
        return SimpleNamespace(source_party=None, source_type=None)

    monkeypatch.setattr(pdf_mod, "process_pdf_record", flaky_process)

    persisted = AsyncMock()
    monkeypatch.setattr(ingest_mod, "_persist_document", persisted)
    monkeypatch.setattr(ingest_mod, "_incr_skipped", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_persist_job_errors", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_finalize_job_if_done", AsyncMock())

    # The job behaves like a real ORM row: readable until rollback expires it,
    # after which any attribute access is a MissingGreenlet-style explosion.
    rolled_back = {"flag": False}
    job = MagicMock()
    type(job).field_mapping = PropertyMock(side_effect=lambda: (
        (_ for _ in ()).throw(AssertionError(
            "job ORM attribute read after rollback — expired-object cascade"))
        if rolled_back["flag"] else {"source_party": "ACME", "source_type": "received"}
    ))
    job.errors = []

    production = MagicMock()
    production.name = "Acme Matter"

    async def rollback():
        rolled_back["flag"] = True

    db = MagicMock()
    db.get = AsyncMock(side_effect=[job, production])
    empty = MagicMock()
    empty.all.return_value = []
    db.execute = AsyncMock(return_value=empty)
    db.commit = AsyncMock()
    db.rollback = MagicMock(side_effect=rollback)

    asyncio.run(ingest_mod.ingest_pdf_batch(db, "job-1", 7, 0, 2))

    # The record after the failure must persist, stamped from the snapshot.
    assert persisted.await_count == 1
    doc = persisted.await_args.args[2]
    assert doc.source_party == "ACME"
    assert doc.source_type == "received"
