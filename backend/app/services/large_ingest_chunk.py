"""Large-container chunk worker: load one chunk's slice of an exploded PST.

One Cloud Task per ``ingest_chunks`` row. The PST was unpacked once by the
``vigilist-pst-explode`` Job; the worker reads the slice's exploded objects
(``exploded_reader``) and never opens the PST. Each message's family commits
on its own; a range that keeps failing is split in half until a single item
fails alone and is recorded as failed.
"""

import asyncio
import logging
import os
import uuid

from sqlalchemy import text

from app.models import IngestChunk, IngestJob
from app.services import exploded_reader, large_ingest
from app.services.large_ingest import split_chunk
from app.services.pst_reader import ContainerSurvey

logger = logging.getLogger(__name__)

# Attempts per chunk before it is split in half (or, at one item, recorded as
# failed). Every claim counts an attempt.
MAX_CHUNK_ATTEMPTS = 3

# Claim token: only one live worker may process a chunk. A 'pending' chunk is
# claimable at once. A 'running' chunk is claimable only once its last claim
# (updated_at) is older than any request could still be alive: the worker's
# Cloud Run request timeout (1800 s, backend-deploy.yml; it equals the Cloud
# Tasks dispatch deadline), plus a margin. So a swept duplicate,
# or a Cloud Tasks retry after the dispatch deadline, can never run alongside
# a live-but-slow original.
#
# Timeline: a worker that raises releases its claim (status back to
# 'pending'), so Cloud Tasks' retry re-claims it within seconds. A worker that
# dies outright (OOM, instance kill, timeout) can't release: Cloud Tasks'
# retries get 503 (``ChunkBusy``) and back off until CHUNK_STALE_S has passed
# since the claim, then the next retry re-claims it (or finalize's orphan
# sweep puts it back to 'pending' and re-sends it).
# Must match the vigilist-worker `--timeout` in backend-deploy.yml.
WORKER_REQUEST_TIMEOUT_S = 1800
CHUNK_STALE_S = WORKER_REQUEST_TIMEOUT_S + 300  # 2100 s

_CLAIM_CHUNK_SQL = (
    "UPDATE ingest_chunks SET attempts = attempts + 1, status = 'running', updated_at = now() "
    "WHERE id = :id AND (status = 'pending' OR "
    "(status = 'running' AND updated_at < now() - make_interval(secs => :stale))) "
    "RETURNING attempts"
)
# The claim holder gives the chunk back after an error so the retry can claim
# it at once (the attempt still counts). A raised error is not a crash, so the
# in-flight position is cleared: the retry must not skip that item.
_RELEASE_CHUNK_SQL = (
    "UPDATE ingest_chunks SET status = 'pending', cursor = NULL, cursor_hits = 0, "
    "updated_at = now() WHERE id = :id AND status = 'running'"
)
# Written (and committed) before each item is built, with the number of
# consecutive attempts that started this same item. If the worker dies while
# building it (OOM, instance kill, timeout), both survive. After POISON_DEATHS
# deaths at one position the next claim skips exactly that item, instead of
# failing the whole chunk three times and halving it down to one item (~a day
# of stale windows). One death is not enough: it may have been an unrelated
# instance kill. Does not touch updated_at, which is the claim time.
_SET_CURSOR_SQL = (
    "UPDATE ingest_chunks SET cursor = :pos, cursor_hits = :hits "
    "WHERE id = :id AND status = 'running'"
)
POISON_DEATHS = 2


class ChunkBusy(Exception):
    """Another live attempt holds this chunk; the task should retry later."""


class ChunkClaimError(Exception):
    """The claim itself failed (e.g. the database was unavailable), so no
    attempt was counted on the chunk row."""


_CHUNK_DONE_SQL = (
    "UPDATE ingest_chunks SET status = 'done', error = NULL, updated_at = now() "
    "WHERE id = :id AND status = 'running'"
)
_CHUNK_SPLIT_SQL = (
    "UPDATE ingest_chunks SET status = 'split', error = :err, updated_at = now() "
    "WHERE id = :id AND status = 'running'"
)
_CHUNK_FAILED_SQL = (
    "UPDATE ingest_chunks SET status = 'failed', error = :err, updated_at = now() "
    "WHERE id = :id AND status = 'running'"
)


def message_control(container_control: str, position: int, total: int) -> str:
    """Control number of the message at 1-based global selected ``position``."""
    w = max(4, len(str(total)))
    return f"{container_control} -{position:0{w}d}"


def _snapshot_chunk(chunk):
    """Plain copy of the chunk's fields: a mid-chunk rollback expires ORM
    objects, and a lazy refresh on the async session would MissingGreenlet."""
    from types import SimpleNamespace

    return SimpleNamespace(
        id=chunk.id, job_id=str(chunk.job_id), kind=chunk.kind,
        container_path=chunk.container_path,
        slices=[list(s) for s in (chunk.slices or [])],
        type_filter=list(chunk.type_filter or []),
        global_offset=int(chunk.global_offset or 0), item_count=int(chunk.item_count or 0),
        doc_ids=[str(d) for d in (getattr(chunk, "doc_ids", None) or [])],
        cursor=getattr(chunk, "cursor", None),
        cursor_hits=int(getattr(chunk, "cursor_hits", None) or 0),
    )


def _folder_path(survey_json: dict | None, folder_index: int) -> str:
    for f in (survey_json or {}).get("folders", []):
        if f.get("index") == folder_index:
            return f.get("path") or ""
    return f"folder {folder_index}"


def _next_selected(gen, type_filter: set[str] | None):
    """Advance ``iter_slice`` to the next selected item (blocking Storage I/O)."""
    for _seq, info, raw_pos, item in gen:
        kind = exploded_reader.item_kind(item)
        if type_filter and kind not in type_filter:
            continue
        return info, raw_pos, item, kind
    return None


def _subject_of(item) -> str:
    try:
        return str(getattr(item, "subject", None) or "").strip()
    except Exception:
        return ""


def _build_item(item, kind: str, control: str, folder_path: str, production_id: int,
                source_path: str, custodian):
    """One exploded item -> ([parent, *attachments], read errors) (blocking:
    fetch, extract, store)."""
    from app.services import ingest_native
    from app.services.ingest_pdf import _ocr_jpeg_text

    parsed = exploded_reader.to_parsed(item, kind)
    docs = ingest_native.build_email_documents(
        parsed,
        message_control=control,
        production_id=production_id,
        source_path=source_path,
        custodian=custodian,
        msg_bytes=ingest_native._serialize_message(parsed),
        # No container native_path: under parallel chunks the container-level
        # dedup would skip the whole PST once any chunk committed.
        native_path=None,
        ocr_fn=_ocr_jpeg_text,
        store_fn=ingest_native._store_attachment,
    )
    for i, doc in enumerate(docs):
        meta = {**(doc.metadata_ or {}), "PST Folder": folder_path}
        if i == 0 and kind != "email":
            meta["PST Item Type"] = kind
        if i == 0 and parsed.read_errors:
            # Partly read: kept with what could be read; say what could not.
            meta["Read Errors"] = "; ".join(parsed.read_errors)[:2000]
        doc.metadata_ = meta
    return docs, list(parsed.read_errors)


def _read_subject_at(job_id: str, production_id: int, storage_path: str, slices) -> str:
    """Subject of a one-item chunk's item: a single GET of its JSON. A
    one-item chunk's slice starts at its (selected) item."""
    folder_index, position = slices[0][0], slices[0][1]
    return exploded_reader.read_subject(job_id, production_id, storage_path, folder_index, position)


async def _split(db, c, survey_json: dict | None) -> None:
    from app.services import tasks
    from app.services.ingest import append_job_errors

    plans = split_chunk(ContainerSurvey.from_json(survey_json), c)
    res = await db.execute(text(_CHUNK_SPLIT_SQL), {
        "id": c.id, "err": f"split after {MAX_CHUNK_ATTEMPTS} failed attempts",
    })
    if res.rowcount != 1:
        # A concurrent dispatch of this chunk already split or finished it.
        await db.rollback()
        return
    children = [
        IngestChunk(
            id=uuid.uuid4(), job_id=c.job_id, kind=c.kind, container_path=c.container_path,
            slices=p["slices"], type_filter=p.get("type_filter", []),
            global_offset=p["global_offset"], item_count=p["item_count"],
            status="pending", attempts=0, parent_id=c.id,
        )
        for p in plans
    ]
    for child in children:
        db.add(child)
    await db.commit()
    errors = []
    for child in children:
        try:
            tasks.enqueue_chunk(str(child.id))
        except Exception as e:
            logger.exception("Failed to enqueue split chunk %s", child.id)
            errors.append(f"Enqueue failed for chunk {child.id}: {e}")
    await append_job_errors(db, c.job_id, errors)
    logger.warning("Chunk %s split into %s after repeated failures",
                   c.id, [p["item_count"] for p in plans])


async def _fail_single(db, c, sel: dict, survey_json: dict | None, attempts: int,
                       production_id: int) -> None:
    """Record a one-item chunk that keeps failing as a failed item, unless
    the item was loaded after all (e.g. by a concurrent attempt): then the
    chunk is simply done."""
    from sqlalchemy import select

    from app.models import Document
    from app.services.ingest import append_job_errors, incr_job_items

    control = message_control(sel.get("control", ""), c.global_offset + 1, int(sel.get("items") or 1))
    loaded = (await db.execute(
        select(Document.id).where(
            Document.production_id == production_id,
            Document.bates_begin == control,
        )
    )).first() is not None
    if loaded:
        await db.execute(text(_CHUNK_DONE_SQL), {"id": c.id})
        await db.commit()
        return
    await db.commit()

    folder_index, raw_pos = c.slices[0][0], c.slices[0][1]
    folder_path = _folder_path(survey_json, folder_index)
    subject = ""
    if attempts == MAX_CHUNK_ATTEMPTS + 1:
        # Try once: reading the item may be exactly what kills the worker.
        try:
            subject = await asyncio.to_thread(
                _read_subject_at, c.job_id, production_id, c.container_path, c.slices)
        except Exception:
            logger.exception("Could not read subject of failed item in chunk %s", c.id)
    msg = (f"{control}: could not be read after {MAX_CHUNK_ATTEMPTS} attempts "
           f"(PST folder {folder_path!r}, position {raw_pos}"
           + (f", subject {subject!r}" if subject else "") + ")")
    res = await db.execute(text(_CHUNK_FAILED_SQL), {"id": c.id, "err": msg})
    if res.rowcount != 1:
        await db.rollback()
        return
    # Status, error and counter commit together: recorded exactly once.
    await append_job_errors(db, c.job_id, [msg], commit=False)
    await incr_job_items(db, c.job_id, failed=1, commit=False)
    await db.commit()


async def _load_items(db, c, job) -> tuple[list[str], int]:
    """Load the chunk's items. Returns (failure messages, failed item count),
    written by the caller with the done transition so a crash-retry can't
    record a failure twice or keep one for an item a retry loaded."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.models import Document
    from app.services.ingest import (
        _persist_documents,
        _source_stamp,
        _stamp_source,
    )

    sel = (job.selection or {}).get(c.container_path) or {}
    container_control = sel["control"]
    total = int(sel.get("items") or c.item_count)
    source_path = sel.get("relative_path") or os.path.basename(c.container_path)
    production_id = job.production_id
    custodian = (job.field_mapping or {}).get("custodian")
    stamp = _source_stamp(job)
    job_id = c.job_id

    controls = [message_control(container_control, c.global_offset + i + 1, total)
                for i in range(c.item_count)]
    existing: set[str] = set()
    if controls:
        rows = (await db.execute(
            select(Document.bates_begin).where(
                Document.production_id == production_id,
                Document.bates_begin.in_(controls),
            )
        )).all()
        existing = {r[0] for r in rows}
    # Release the connection before reading the exploded items.
    await db.commit()

    # Earlier attempts died while building this item: after POISON_DEATHS
    # consecutive deaths at it, skip it now.
    crashed = c.cursor
    if crashed is None or not (0 <= crashed < c.item_count) or controls[crashed] in existing:
        crashed = None
    poison = crashed if crashed is not None and c.cursor_hits >= POISON_DEATHS else None

    type_filter = set(c.type_filter) or None
    local = 0
    failures: list[str] = []
    failed = 0
    gen = exploded_reader.iter_slice(job_id, production_id, c.container_path, c.slices)
    try:
        while local < c.item_count:
            found = await asyncio.to_thread(_next_selected, gen, type_filter)
            if found is None:
                break
            info, raw_pos, item, kind = found
            pos = local
            control = controls[local]
            local += 1
            if control in existing:
                continue
            if pos == poison:
                logger.warning("Chunk %s: skipping %s, the previous attempt died loading it",
                               c.id, control)
                failures.append(f"{control}: skipped; the worker stopped while loading it "
                                f"{POISON_DEATHS} times (PST folder {info.path!r}, position {raw_pos})")
                failed += 1
                continue
            hits = c.cursor_hits + 1 if pos == crashed else 1
            await db.execute(text(_SET_CURSOR_SQL), {"id": c.id, "pos": pos, "hits": hits})
            await db.commit()
            try:
                docs, read_errors = await asyncio.to_thread(
                    _build_item, item, kind, control, info.path, production_id, source_path, custodian)
                for d in docs:
                    _stamp_source(d, stamp)
                await _persist_documents(db, job_id, docs, items_done=1)
                if read_errors:
                    # Loaded, but not every field could be read: reported,
                    # not counted as failed.
                    failures.append(f"{control}: partly read (PST folder {info.path!r}, position "
                                    f"{raw_pos}): " + "; ".join(read_errors)[:1000])
            except Exception as e:
                await db.rollback()
                if isinstance(e, IntegrityError):
                    already = (await db.execute(
                        select(Document.id).where(
                            Document.production_id == production_id,
                            Document.bates_begin == control,
                        )
                    )).first() is not None
                    await db.commit()
                    if already:
                        # A concurrent run of this chunk committed it first (and counted it).
                        continue
                logger.exception("Failed to load %s", control)
                subject = _subject_of(item)
                msg = (f"{control}: could not be loaded (PST folder {info.path!r}, position {raw_pos}"
                       + (f", subject {subject!r}" if subject else "") + f"): {e}")
                failures.append(msg)
                failed += 1
    finally:
        gen.close()

    if local < c.item_count:
        # The slice held fewer selected items than planned: account for every
        # missing position so nothing is silently dropped.
        missing = c.item_count - local
        msg = (f"{controls[local]}..{controls[-1]}: chunk read {local} items, expected "
               f"{c.item_count}; {missing} could not be read")
        failures.append(msg)
        failed += missing
    return failures, failed


async def _release(db, chunk_id) -> None:
    try:
        await db.rollback()
        await db.execute(text(_RELEASE_CHUNK_SQL), {"id": chunk_id})
        await db.commit()
    except Exception:
        # Unreleased, the chunk becomes claimable once CHUNK_STALE_S passes.
        logger.exception("Could not release chunk %s", chunk_id)


async def run_chunk(db, chunk_id: str) -> None:
    """Process one ``ingest_chunks`` row (Cloud Tasks worker).

    Raises ``ChunkBusy`` when another live attempt holds the chunk.
    """
    try:
        attempts = (await db.execute(
            text(_CLAIM_CHUNK_SQL), {"id": chunk_id, "stale": CHUNK_STALE_S})).scalar()
        await db.commit()
    except Exception as e:
        raise ChunkClaimError(f"could not claim chunk {chunk_id}: {e}") from e
    if attempts is None:
        # No attempt was counted, so failures here are pre-claim failures
        # (capped by the persisted per-chunk counter, like a failed claim).
        try:
            chunk = await db.get(IngestChunk, chunk_id, populate_existing=True)
            if chunk is None:
                return
            if chunk.status == "running":
                raise ChunkBusy(f"chunk {chunk_id} is held by another attempt")
            # Done, split or failed. A redelivered task still nudges finalize:
            # a crash between the done commit and maybe_finalize (or inside
            # it, which rolls back) would otherwise leave nobody to call it.
            await large_ingest.maybe_finalize(db, str(chunk.job_id))
        except ChunkBusy:
            raise
        except Exception as e:
            raise ChunkClaimError(f"chunk {chunk_id} (already claimed or finished): {e}") from e
        return
    chunk = await db.get(IngestChunk, chunk_id, populate_existing=True)
    if chunk is None:
        return
    c = _snapshot_chunk(chunk)
    try:
        await _process(db, c, attempts)
    except Exception:
        # No-op once the chunk reached done/split/failed.
        await _release(db, c.id)
        raise


async def _process(db, c, attempts: int) -> None:
    if c.kind in ("embed", "post"):
        from app.services import large_ingest_finalize as fin

        handler = fin.run_embed_chunk if c.kind == "embed" else fin.run_post_chunk
        await handler(db, c, attempts)
        return
    if c.kind != "items":
        logger.warning("Chunk %s has unknown kind %r", c.id, c.kind)
        return
    job = await db.get(IngestJob, c.job_id, populate_existing=True)
    if job is None:
        return
    survey_json = (job.survey or {}).get(c.container_path)

    if attempts > MAX_CHUNK_ATTEMPTS:
        if c.item_count > 1:
            await _split(db, c, survey_json)
            return
        sel = dict((job.selection or {}).get(c.container_path) or {})
        await _fail_single(db, c, sel, survey_json, attempts, job.production_id)
        await large_ingest.maybe_finalize(db, c.job_id)
        return

    from app.services.ingest import append_job_errors, incr_job_items

    failures, failed = await _load_items(db, c, job)
    res = await db.execute(text(_CHUNK_DONE_SQL), {"id": c.id})
    if res.rowcount == 1:
        # Failures commit with the done transition, exactly once per chunk.
        await append_job_errors(db, c.job_id, failures, commit=False)
        if failed:
            await incr_job_items(db, c.job_id, failed=failed, commit=False)
    await db.commit()
    await large_ingest.maybe_finalize(db, c.job_id)
