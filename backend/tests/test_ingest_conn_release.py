"""The batch loop must release its DB connection before long render/OCR work.

Prod incident follow-up (2026-07-25, part 3): the ingest batch session holds
its connection checked out — inside an autobegun transaction — across the
entire ``asyncio.to_thread(render/OCR)`` call. For large documents that call
runs longer than Neon's idle limit, the pooler kills the connection, and the
post-render flush dies with "connection is closed" / asyncpg InterfaceError.
This is why exactly the longest documents of a load failed on every attempt
while short ones sailed through. Same failure mode and same fix as the
timeline-review's commit-before-model-call (PR #87): ``await db.commit()``
before the long work ends the implicit transaction and returns the connection
to the pool; the next DB operation checks out a fresh, pre-pinged one.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services import ingest as ingest_mod


def test_pdf_batch_commits_before_each_render(monkeypatch):
    from app.services import ingest_pdf as pdf_mod

    items = [
        {"storage_path": "productions/7/raw/A/big1.pdf", "relative_path": "A/big1.pdf",
         "filename": "big1.pdf"},
        {"storage_path": "productions/7/raw/B/big2.pdf", "relative_path": "B/big2.pdf",
         "filename": "big2.pdf"},
    ]
    monkeypatch.setattr(pdf_mod, "list_pdf_sources", lambda pid, load_prefix=None: items)

    events: list[str] = []

    def fake_render(*a, **k):
        events.append("work")
        return SimpleNamespace(source_party=None, source_type=None)

    monkeypatch.setattr(pdf_mod, "process_pdf_record", fake_render)
    monkeypatch.setattr(ingest_mod, "_persist_document", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_incr_skipped", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_persist_job_errors", AsyncMock())
    monkeypatch.setattr(ingest_mod, "_finalize_job_if_done", AsyncMock())

    job = MagicMock()
    job.field_mapping = {"control_offset": 0}
    job.errors = []
    production = MagicMock()
    production.name = "Acme Matter"

    async def commit():
        events.append("commit")

    db = MagicMock()
    db.get = AsyncMock(side_effect=[job, production])
    empty = MagicMock()
    empty.all.return_value = []
    db.execute = AsyncMock(return_value=empty)
    db.commit = MagicMock(side_effect=commit)
    db.rollback = AsyncMock()

    asyncio.run(ingest_mod.ingest_pdf_batch(db, "job-1", 7, 0, 2))

    # Every render must be immediately preceded by a commit that returned the
    # session's connection to the pool. (_persist/_incr are mocked, so the
    # only commits observable here are the pre-render releases.)
    assert events == ["commit", "work", "commit", "work"]
