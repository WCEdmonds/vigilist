"""Large-container finalize: reconciliation, fanned-out embeddings, completion.

Called after every chunk (and every normal batch) of a job with large
containers. Two phases, each claimed with one conditional statement so
concurrent callers can't run them twice:

- **Phase A** (``processing`` -> ``finalizing``): once no ``items`` chunk is
  pending or running and every normal source file is attempted. Marks each
  large container's source key done, appends its reconciliation line, sets the
  production's AI hold and writes ``embed`` chunks.
- **Phase B**: once no ``embed`` chunk is pending or running, writes the single
  ``post`` chunk (threading + titles). The post chunk marks the job
  ``complete``. Nothing here enqueues the ambient pipeline: the AI hold keeps
  it off until someone runs analysis.

A ``split`` parent is terminal for these checks: its children are inserted in
the same transaction as the split, and they are the rows that carry the
remaining work.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import settings
from app.models import IngestChunk, IngestJob
from app.services.large_ingest import large_source_key
from app.services.pst_reader import ITEM_TYPES

logger = logging.getLogger(__name__)

# Documents per embed chunk.
EMBED_BATCH_DOCS = 200
# A pending chunk untouched this long, with nothing running, lost its Cloud
# Task (an enqueue that failed): re-enqueue it. Longer than the 30-minute
# dispatch deadline, so a live queued task is not doubled up in practice.
ORPHAN_AFTER_MINUTES = 35
# Re-enqueued per sweep (each is one Cloud Tasks create call).
ORPHAN_SWEEP_LIMIT = 500
# AI titles in the post chunk are capped so it finishes well inside the
# dispatch deadline. Emails are titled by subject and attachments by filename
# at load time, so few documents reach this; the rest keep the filename
# fallback.
POST_AI_TITLE_LIMIT = 200

ACTIVE = ("pending", "running")

_CHUNK_SUMMARY_SQL = (
    "SELECT kind, status, count(*) AS n FROM ingest_chunks "
    "WHERE job_id = :jid GROUP BY kind, status"
)
_CLAIM_FINALIZE_SQL = (
    "UPDATE ingest_jobs SET status = 'finalizing' WHERE id = :id AND status = 'processing'"
)
_MARK_SOURCE_DONE_SQL = (
    "UPDATE ingest_jobs SET done_keys = done_keys || to_jsonb(cast(:key as text)) "
    "WHERE id = :jid AND NOT (done_keys @> to_jsonb(cast(:key as text)))"
)
# Parent messages of one container: ``"{container control} -{position}"``,
# whose family_id is their own control number (attachments are
# ``"... .0001"`` and carry the parent's family_id).
_LOADED_MESSAGES_SQL = (
    "SELECT count(*) FROM documents WHERE production_id = :pid "
    "AND bates_begin LIKE :pat ESCAPE '\\' AND family_id = bates_begin"
)
_AI_HOLD_SQL = "UPDATE productions SET ai_hold = true WHERE id = :pid"
# Same criterion as embeddings.embed_production_documents (what the normal
# finalize embeds): the production's documents with text and no chunks. That
# covers every document this job loaded (large containers and normal files)
# plus any earlier document whose embedding never landed.
_EMBED_DOCS_SQL = (
    "SELECT d.id FROM documents d WHERE d.production_id = :pid "
    "AND d.text_content IS NOT NULL AND d.text_content <> '' "
    "AND NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.document_id = d.id) "
    "ORDER BY d.id"
)
# Serializes Phase B callers: the post-chunk existence check below runs after
# the lock is granted, so it sees a concurrent caller's committed insert.
_LOCK_FINALIZING_SQL = (
    "SELECT id FROM ingest_jobs WHERE id = :jid AND status = 'finalizing' FOR UPDATE"
)
# Explicit casts: a bare parameter in an INSERT ... SELECT target list
# resolves to text, which Postgres won't assign to a uuid column.
_INSERT_POST_SQL = (
    "INSERT INTO ingest_chunks (id, job_id, kind, status, attempts) "
    "SELECT cast(:cid as uuid), cast(:jid as uuid), 'post', 'pending', 0 "
    "WHERE NOT EXISTS (SELECT 1 FROM ingest_chunks "
    "WHERE job_id = cast(:jid as uuid) AND kind = 'post') "
    "RETURNING id"
)
# Touching updated_at claims the re-enqueue: a second sweep inside the window
# finds nothing.
_SWEEP_ORPHANS_SQL = (
    "UPDATE ingest_chunks SET updated_at = now() WHERE id IN ("
    "SELECT id FROM ingest_chunks WHERE job_id = :jid AND status = 'pending' "
    "AND updated_at < now() - make_interval(mins => :mins) "
    "ORDER BY created_at LIMIT :lim FOR UPDATE SKIP LOCKED) RETURNING id"
)
_COMPLETE_JOB_SQL = (
    "UPDATE ingest_jobs SET status = 'complete', completed_at = :now "
    "WHERE id = :jid AND status = 'finalizing'"
)
_HAS_CHUNKS_SQL = "SELECT 1 FROM document_chunks WHERE document_id = :id LIMIT 1"
_DOC_CONTROL_SQL = "SELECT bates_begin FROM documents WHERE id = :id"


def _now():
    # Naive UTC to match the tz-naive completed_at column.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _as_uuid(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return value


async def _chunk_summary(db, job_id: str) -> dict[tuple[str, str], int]:
    rows = (await db.execute(text(_CHUNK_SUMMARY_SQL), {"jid": job_id})).all()
    return {(kind, status): int(n) for kind, status, n in rows}


def _active(summary: dict, kind: str | None = None) -> int:
    return sum(n for (k, s), n in summary.items() if s in ACTIVE and (kind is None or k == kind))


def _normal_sources_attempted(job, large: list[str]) -> bool:
    """Every source file other than the large containers is done or skipped."""
    large_keys = {large_source_key(p) for p in large}
    attempted = (set(job.done_keys or []) | set(job.skipped_keys or [])) - large_keys
    return len(attempted) >= int(job.total_files or 0) - len(large)


def _enqueue_all(chunk_ids) -> list[str]:
    from app.services import tasks

    errors = []
    for cid in chunk_ids:
        try:
            tasks.enqueue_chunk(str(cid))
        except Exception as e:
            logger.exception("Failed to enqueue chunk %s", cid)
            errors.append(f"Enqueue failed for chunk {cid}: {e}")
    return errors


async def _sweep_orphans(db, job_id: str) -> int:
    from app.services.ingest import append_job_errors

    rows = (await db.execute(text(_SWEEP_ORPHANS_SQL), {
        "jid": job_id, "mins": ORPHAN_AFTER_MINUTES, "lim": ORPHAN_SWEEP_LIMIT,
    })).all()
    await db.commit()
    ids = [r[0] for r in rows]
    if ids:
        logger.warning("Job %s: re-enqueuing %d orphaned pending chunks", job_id, len(ids))
        await append_job_errors(db, job_id, _enqueue_all(ids))
    return len(ids)


def _exclusions(survey_json: dict, sel: dict) -> str:
    """``"contact 4, task 2"``: what the uploader left out, by type."""
    selected = set(sel.get("types") or [])
    include_recoverable = bool(sel.get("include_recoverable", True))
    totals: dict[str, int] = {}
    recoverable = 0
    for f in (survey_json or {}).get("folders", []):
        for t, n in (f.get("counts") or {}).items():
            n = int(n)
            if t in selected:
                if f.get("recoverable") and not include_recoverable:
                    recoverable += n
            else:
                totals[t] = totals.get(t, 0) + n
    order = {t: i for i, t in enumerate(ITEM_TYPES)}
    parts = [f"{t} {n:,}" for t, n in sorted(totals.items(), key=lambda kv: order.get(kv[0], 99)) if n]
    if recoverable:
        parts.append(f"recoverable items folders {recoverable:,}")
    return ", ".join(parts) or "none"


async def _reconciliation_line(db, job, path: str) -> str:
    sel = (job.selection or {}).get(path) or {}
    name = os.path.basename(sel.get("relative_path") or path)
    selected = int(sel.get("items") or 0)
    loaded = 0
    if sel.get("control"):
        loaded = int((await db.execute(text(_LOADED_MESSAGES_SQL), {
            "pid": job.production_id, "pat": f"{_like_escape(sel['control'])} -%",
        })).scalar() or 0)
    # Everything selected but not loaded could not be read, so the line
    # always adds up to the selection.
    failed = max(selected - loaded, 0)
    excluded = _exclusions((job.survey or {}).get(path) or {}, sel)
    return (f"{name}: {loaded:,} of {selected:,} loaded, {failed:,} could not be read; "
            f"excluded by uploader: {excluded}; deep recovery of orphaned items was not performed")


async def _embed_doc_ids(db, production_id: int) -> list[str]:
    if not settings.voyage_api_key:
        logger.info("VIGILIST_VOYAGE_API_KEY not set — no embed chunks")
        return []
    rows = (await db.execute(text(_EMBED_DOCS_SQL), {"pid": production_id})).all()
    return [str(r[0]) for r in rows]


async def _phase_a(db, job, large: list[str]) -> None:
    from app.services.ingest import append_job_errors

    job_id = str(job.id)
    res = await db.execute(text(_CLAIM_FINALIZE_SQL), {"id": job_id})
    if res.rowcount != 1:
        await db.rollback()
        return  # another caller claimed it
    try:
        for path in large:
            await db.execute(text(_MARK_SOURCE_DONE_SQL), {"key": large_source_key(path), "jid": job_id})
        lines = [await _reconciliation_line(db, job, path) for path in large]
        await append_job_errors(db, job_id, lines, commit=False)
        await db.execute(text(_AI_HOLD_SQL), {"pid": job.production_id})
        doc_ids = await _embed_doc_ids(db, job.production_id)
        chunks = []
        for i in range(0, len(doc_ids), EMBED_BATCH_DOCS):
            batch = doc_ids[i:i + EMBED_BATCH_DOCS]
            chunks.append(IngestChunk(
                id=uuid.uuid4(), job_id=job.id, kind="embed", doc_ids=batch,
                item_count=len(batch), status="pending", attempts=0,
            ))
        for chunk in chunks:
            db.add(chunk)
        # Claim, keys, reconciliation, hold and embed chunks commit together:
        # a crash before this leaves the job 'processing' for a retry to claim.
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    logger.info("Job %s finalizing: %d embed chunks over %d documents",
                job_id, len(chunks), len(doc_ids))
    if chunks:
        await append_job_errors(db, job_id, _enqueue_all([c.id for c in chunks]))
    else:
        await _phase_b(db, job_id)


async def _phase_b(db, job_id: str) -> None:
    from app.services.ingest import append_job_errors

    locked = (await db.execute(text(_LOCK_FINALIZING_SQL), {"jid": job_id})).first()
    if locked is None:
        await db.rollback()
        return
    cid = (await db.execute(text(_INSERT_POST_SQL), {"cid": str(uuid.uuid4()), "jid": str(job_id)})).scalar()
    await db.commit()
    if cid is not None:
        logger.info("Job %s: embeddings done, post chunk %s", job_id, cid)
        await append_job_errors(db, job_id, _enqueue_all([cid]))


async def maybe_finalize(db, job_id: str) -> None:
    """Advance a large-container job's finalize if it is ready (idempotent)."""
    job = await db.get(IngestJob, job_id, populate_existing=True)
    if job is None:
        return
    large = list((job.field_mapping or {}).get("large_containers") or [])
    if not large or job.status not in ("processing", "finalizing"):
        return
    summary = await _chunk_summary(db, job_id)
    await db.commit()  # release the connection between steps

    if _active(summary) and not any(s == "running" for (_k, s) in summary):
        # Everything left is pending with nothing running: a chunk whose
        # enqueue failed would wait forever. Re-enqueue stale ones (run_chunk
        # claims atomically, so a duplicate dispatch is harmless). Pending
        # work blocks both phases, so there is nothing to finalize yet.
        await _sweep_orphans(db, job_id)
        return

    if job.status == "processing":
        if _active(summary, "items") or not _normal_sources_attempted(job, large):
            return
        await _phase_a(db, job, large)
    else:
        if _active(summary, "embed") or _active(summary, "post"):
            return
        await _phase_b(db, job_id)


# --- embed / post chunk handlers (dispatched by large_ingest_chunk.run_chunk) ---


async def _split_embed(db, c) -> None:
    from app.services.ingest import append_job_errors
    from app.services.large_ingest_chunk import _CHUNK_SPLIT_SQL, MAX_CHUNK_ATTEMPTS

    res = await db.execute(text(_CHUNK_SPLIT_SQL), {
        "id": c.id, "err": f"split after {MAX_CHUNK_ATTEMPTS} failed attempts",
    })
    if res.rowcount != 1:
        await db.rollback()
        return
    half = (len(c.doc_ids) + 1) // 2
    children = [
        IngestChunk(
            id=uuid.uuid4(), job_id=c.job_id, kind="embed", doc_ids=part,
            item_count=len(part), status="pending", attempts=0, parent_id=c.id,
        )
        for part in (c.doc_ids[:half], c.doc_ids[half:]) if part
    ]
    for child in children:
        db.add(child)
    await db.commit()
    await append_job_errors(db, c.job_id, _enqueue_all([ch.id for ch in children]))


async def _fail_embed_single(db, c) -> None:
    from app.services.ingest import append_job_errors
    from app.services.large_ingest_chunk import _CHUNK_FAILED_SQL, MAX_CHUNK_ATTEMPTS

    doc_id = c.doc_ids[0] if c.doc_ids else ""
    control = None
    try:
        control = (await db.execute(text(_DOC_CONTROL_SQL), {"id": _as_uuid(doc_id)})).scalar()
    except Exception:
        await db.rollback()
    msg = (f"{control or f'Document {doc_id}'}: search embeddings could not be generated "
           f"after {MAX_CHUNK_ATTEMPTS} attempts")
    res = await db.execute(text(_CHUNK_FAILED_SQL), {"id": c.id, "err": msg})
    if res.rowcount != 1:
        await db.rollback()
        return
    await append_job_errors(db, c.job_id, [msg], commit=False)
    await db.commit()


async def run_embed_chunk(db, c, attempts: int) -> None:
    """Embed the chunk's documents one at a time, committing each.

    A document that raises fails the request so Cloud Tasks retries; documents
    already embedded are skipped on the retry. Past ``MAX_CHUNK_ATTEMPTS`` the
    chunk is halved, and a single document that still fails is recorded and
    dropped so completion is never blocked.
    """
    from app.services import embeddings
    from app.services.large_ingest_chunk import _CHUNK_DONE_SQL, MAX_CHUNK_ATTEMPTS

    if attempts > MAX_CHUNK_ATTEMPTS:
        if len(c.doc_ids) > 1:
            await _split_embed(db, c)
            return
        await _fail_embed_single(db, c)
        await maybe_finalize(db, c.job_id)
        return

    failed = 0
    for doc_id in c.doc_ids:
        doc_uuid = _as_uuid(doc_id)
        has = (await db.execute(text(_HAS_CHUNKS_SQL), {"id": doc_uuid})).first()
        await db.commit()
        if has is not None:
            continue
        try:
            await embeddings.chunk_and_embed_document(db, doc_uuid)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Embedding failed for doc %s in chunk %s", doc_id, c.id)
            failed += 1
    if failed:
        raise RuntimeError(f"embedding failed for {failed} of {len(c.doc_ids)} documents")

    await db.execute(text(_CHUNK_DONE_SQL), {"id": c.id})
    await db.commit()
    await maybe_finalize(db, c.job_id)


async def run_post_chunk(db, c, attempts: int) -> None:
    """Threading and titles, then mark the job complete. Never enqueues the
    ambient pipeline (the production is on AI hold)."""
    from app.services.ingest import append_job_errors, generate_missing_titles
    from app.services.large_ingest_chunk import (
        _CHUNK_DONE_SQL,
        _CHUNK_FAILED_SQL,
        MAX_CHUNK_ATTEMPTS,
    )

    job = await db.get(IngestJob, c.job_id, populate_existing=True)
    if job is None:
        return
    production_id = job.production_id
    await db.commit()

    if attempts > MAX_CHUNK_ATTEMPTS:
        msg = (f"Email threading and titles did not finish after {MAX_CHUNK_ATTEMPTS} attempts; "
               "the load is complete without them")
        res = await db.execute(text(_CHUNK_FAILED_SQL), {"id": c.id, "err": msg})
        if res.rowcount == 1:
            await append_job_errors(db, c.job_id, [msg], commit=False)
            await db.execute(text(_COMPLETE_JOB_SQL), {"jid": c.job_id, "now": _now()})
        await db.commit()
        return

    errors: list[str] = []
    try:
        from app.services import email_threading

        await email_threading.derive_threads(db, production_id)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("thread derivation skipped for production %s", production_id)
    try:
        await generate_missing_titles(db, production_id, errors, ai_limit=POST_AI_TITLE_LIMIT)
    except Exception as e:
        await db.rollback()
        logger.exception("Title generation skipped for production %s", production_id)
        errors.append(f"Title generation skipped: {e}")

    res = await db.execute(text(_CHUNK_DONE_SQL), {"id": c.id})
    if res.rowcount == 1:
        # Done transition, warnings and completion commit together.
        await append_job_errors(db, c.job_id, errors, commit=False)
        await db.execute(text(_COMPLETE_JOB_SQL), {"jid": c.job_id, "now": _now()})
    await db.commit()
    logger.info("Large-container job %s complete", c.job_id)
