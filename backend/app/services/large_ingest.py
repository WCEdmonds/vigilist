"""Large-container (multi-GB PST/OST) ingest: detection, survey, selection.

Containers at or above ``settings.large_container_bytes`` skip the readpst
batch path. They are surveyed first (cheap, read-only walk of the folder tree)
so the user can see counts and cost before any paid work starts. The
uploader's type selection is then planned into fixed chunks, one Cloud Task
each.
"""

import asyncio
import logging
import os
import uuid

from sqlalchemy import text

from app.config import settings
from app.models import IngestChunk, IngestJob, Production
from app.services import pst_reader
from app.services.large_ingest_estimate import estimate_load
from app.services.pst_reader import ITEM_TYPES, ContainerSurvey

logger = logging.getLogger(__name__)

LARGE_CONTAINER_EXTS = {".pst", ".ost"}


# Import-time snapshot for display; `is_large_container` reads settings live.
LARGE_CONTAINER_BYTES = settings.large_container_bytes


def is_large_container(item: dict) -> bool:
    ext = os.path.splitext(item.get("filename") or "")[1].lower()
    return ext in LARGE_CONTAINER_EXTS and int(item.get("size") or 0) >= settings.large_container_bytes


def large_source_key(storage_path: str) -> str:
    return f"native:{storage_path}"


def _survey_container(storage_path: str) -> dict:
    with pst_reader.open_pst(storage_path) as handle:
        result = pst_reader.survey(handle)
    result.storage_path = storage_path
    return result.to_json()


async def run_survey(db, job_id: str, storage_path: str) -> None:
    """Survey one container and record it on the job.

    When every large container in the job has a survey, the job moves to
    ``awaiting_selection``.
    """
    job = await db.get(IngestJob, job_id)
    if not job:
        return
    # Release the connection for the (minutes-long) container walk.
    await db.commit()

    survey_json = await asyncio.to_thread(_survey_container, storage_path)

    # Re-read under a row lock so two surveys finishing together can't lose
    # each other's write or both miss the "all surveyed" transition.
    # Sessions use expire_on_commit=False, so without populate_existing the
    # identity map would hand back the stale pre-survey object.
    job = await db.get(IngestJob, job_id, with_for_update=True, populate_existing=True)
    surveys = dict(job.survey or {})
    surveys[storage_path] = survey_json
    job.survey = surveys
    expected = (job.field_mapping or {}).get("large_containers") or []
    if all(p in surveys for p in expected) and job.status == "surveying":
        job.status = "awaiting_selection"
    await db.commit()


# Selected items per chunk. Provisional until the U1 throughput spike sizes it
# to finish well inside the 30-minute dispatch deadline.
CHUNK_ITEMS = 500


async def maybe_finalize(db, job_id: str) -> None:
    """Replaced in Unit 5."""
    return None


class SelectionError(Exception):
    """A selection request that can't be applied; ``status`` is the HTTP code."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _folder_selected_runs(folder, types: set[str]) -> list[tuple[int, int]]:
    """Raw ``[start, end)`` runs of selected items in one folder."""
    total = sum(int(n) for n in folder.counts.values())
    selected = sum(int(n) for t, n in folder.counts.items() if t in types)
    if selected == 0:
        return []
    if selected == total:
        return [(0, total)]
    if not folder.runs:
        raise ValueError(
            f"survey of folder {folder.path!r} has no item positions; re-survey the container"
        )
    out: list[tuple[int, int]] = []
    pos = 0
    for kind, n in folder.runs:
        if kind in types:
            if out and out[-1][1] == pos:
                out[-1] = (out[-1][0], pos + n)
            else:
                out.append((pos, pos + n))
        pos += n
    return out


def _segments(survey: ContainerSurvey, slices, types: set[str] | None):
    """Selected raw segments ``(folder_index, start, end)`` inside ``slices``,
    in traversal order. ``types=None`` means every position in ``slices`` is
    selected (an unfiltered chunk)."""
    by_index = {f.index: f for f in survey.folders}
    for fi, start, end in slices:
        if types is None:
            if start < end:
                yield (fi, start, end)
            continue
        for run_start, run_end in _folder_selected_runs(by_index[fi], types):
            lo, hi = max(start, run_start), min(end, run_end)
            if lo < hi:
                yield (fi, lo, hi)


def _pack(segments, chunk_items: int, types: set[str] | None, start_offset: int = 0) -> list[dict]:
    """Cut a stream of selected segments into chunks of ``chunk_items``.

    Adjacent segments in the same folder merge into one raw slice; when that
    bridges unselected items the chunk gets a ``type_filter`` to drop them at
    read time.
    """
    chunks: list[dict] = []
    current: list[list[int]] = []
    count = 0
    offset = start_offset

    def close():
        nonlocal current, count, offset
        if not count:
            return
        slices: list[list[int]] = []
        gap = False
        for fi, s, e in current:
            if slices and slices[-1][0] == fi:
                gap = gap or slices[-1][2] != s
                slices[-1][2] = e
            else:
                slices.append([fi, s, e])
        chunk = {"slices": slices, "global_offset": offset, "item_count": count}
        if gap and types is not None:
            chunk["type_filter"] = sorted(types)
        chunks.append(chunk)
        offset += count
        current, count = [], 0

    for fi, s, e in segments:
        while s < e:
            take = min(e - s, chunk_items - count)
            current.append([fi, s, s + take])
            count += take
            s += take
            if count == chunk_items:
                close()
    close()
    return chunks


def plan_chunks(survey: ContainerSurvey, types: set[str], include_recoverable: bool,
                chunk_items: int = CHUNK_ITEMS) -> list[dict]:
    """Split the selected items into chunks with deterministic global offsets.

    ``slices`` count raw folder positions; ``item_count``/``global_offset``
    count selected items only.
    """
    types = set(types)
    if not types:
        return []
    full = [
        [f.index, 0, sum(int(n) for n in f.counts.values())]
        for f in sorted(survey.folders, key=lambda f: f.index)
        if include_recoverable or not f.recoverable
    ]
    return _pack(_segments(survey, full, types), chunk_items, types)


def split_chunk(survey: ContainerSurvey, chunk) -> list[dict]:
    """Halve a chunk's selected range into two child plans."""
    types = set(chunk.type_filter) if chunk.type_filter else None
    half = (int(chunk.item_count) + 1) // 2
    return _pack(_segments(survey, chunk.slices, types), half, types, int(chunk.global_offset))


def _container_identity(production_id: int, fm: dict, prefix: str, storage_path: str):
    """(container control, relative path) from the upload's source order,
    numbered exactly as ``ingest_native_batch`` numbers it."""
    from app.services.ingest_native import list_native_sources

    offset = int(fm.get("control_offset") or 0)
    for index, item in enumerate(list_native_sources(production_id, fm.get("load_prefix"))):
        if item["storage_path"] == storage_path:
            return f"{prefix} {offset + index + 1:06d}", item["relative_path"]
    return None


async def apply_selection(db, job_id: str, storage_path: str, types: list[str],
                          include_recoverable: bool) -> dict:
    """Record the uploader's selection for one container, write its chunks and
    enqueue them. Returns the estimate."""
    from app.services import tasks
    from app.services.ingest import append_job_errors
    from app.services.ingest_pdf import derive_bates_prefix

    unknown = sorted(set(types) - set(ITEM_TYPES))
    if unknown:
        raise SelectionError(422, f"unknown item types: {', '.join(unknown)}")

    job = await db.get(IngestJob, job_id)
    if not job:
        raise SelectionError(404, "Job not found")
    fm = dict(job.field_mapping or {})
    production_id = job.production_id
    if storage_path not in (fm.get("large_containers") or []) or storage_path not in (job.survey or {}):
        raise SelectionError(404, "Container not surveyed")
    production = await db.get(Production, production_id)
    prefix = derive_bates_prefix(production.name if production else "")
    # Release the connection for the storage listing.
    await db.commit()
    identity = await asyncio.to_thread(_container_identity, production_id, fm, prefix, storage_path)
    if identity is None:
        raise SelectionError(404, "Container is no longer in the upload")
    control, relative_path = identity

    # Authoritative checks under the row lock: two selections racing (or two
    # containers' selections) must not lose each other's write.
    job = await db.get(IngestJob, job_id, with_for_update=True, populate_existing=True)
    if storage_path in (job.selection or {}):
        raise SelectionError(409, "Selection already applied for this container")
    if job.status != "awaiting_selection":
        raise SelectionError(409, f"Job is {job.status}, not awaiting selection")

    type_set = set(types)
    survey = ContainerSurvey.from_json(job.survey[storage_path])
    plans = plan_chunks(survey, type_set, include_recoverable, CHUNK_ITEMS)
    estimate = estimate_load(survey, type_set, include_recoverable)
    selected = sum(p["item_count"] for p in plans)

    selection = dict(job.selection or {})
    selection[storage_path] = {
        "types": sorted(type_set),
        "include_recoverable": bool(include_recoverable),
        "control": control,
        "relative_path": relative_path,
        "items": selected,
        "estimate": estimate,
    }
    job.selection = selection
    job.items_total = int(job.items_total or 0) + selected
    if all(p in selection for p in fm.get("large_containers") or []):
        job.status = "processing"

    chunks = [
        IngestChunk(
            id=uuid.uuid4(), job_id=job.id, kind="items", container_path=storage_path,
            slices=p["slices"], type_filter=p.get("type_filter", []),
            global_offset=p["global_offset"], item_count=p["item_count"],
            status="pending", attempts=0,
        )
        for p in plans
    ]
    for chunk in chunks:
        db.add(chunk)
    # Chunks must be durable before their tasks can dispatch.
    await db.commit()

    errors: list[str] = []
    for chunk in chunks:
        try:
            tasks.enqueue_chunk(str(chunk.id))
        except Exception as e:
            logger.exception("Failed to enqueue chunk %s", chunk.id)
            errors.append(f"Enqueue failed for chunk {chunk.id} of {relative_path}: {e}")
    if errors:
        await append_job_errors(db, job_id, errors)
    if not chunks:
        # Nothing to load from this container, so no chunk will ever call
        # finalize; call it here or the job would wait forever.
        await maybe_finalize(db, job_id)
    logger.info("Selection for job %s %s: %d items in %d chunks",
                job_id, storage_path, selected, len(chunks))
    return estimate


# --- chunk worker -------------------------------------------------------------

# Attempts per chunk before it is split in half (or, at one item, recorded as
# failed). A worker crash (OOM, timeout) leaves the chunk 'running'; the Cloud
# Tasks retry re-claims it and the attempt count drives the split.
MAX_CHUNK_ATTEMPTS = 3

_CLAIM_CHUNK_SQL = (
    "UPDATE ingest_chunks SET attempts = attempts + 1, status = 'running', updated_at = now() "
    "WHERE id = :id AND status IN ('pending', 'running') RETURNING attempts"
)
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
    )


def _folder_path(survey_json: dict | None, folder_index: int) -> str:
    for f in (survey_json or {}).get("folders", []):
        if f.get("index") == folder_index:
            return f.get("path") or ""
    return f"folder {folder_index}"


def _next_selected(gen, type_filter: set[str] | None):
    """Advance ``iter_slice`` to the next selected item (blocking pypff I/O)."""
    for _seq, info, raw_pos, item in gen:
        kind = pst_reader.item_kind(item)
        if type_filter and kind not in type_filter:
            continue
        return info, raw_pos, item, kind
    return None


def _subject_of(item) -> str:
    try:
        return pst_reader._text(getattr(item, "subject", None)).strip()
    except Exception:
        return ""


def _build_item(item, kind: str, control: str, folder_path: str, production_id: int,
                source_path: str, custodian):
    """One PST item -> [parent, *attachments] (blocking: parse, extract, store)."""
    from app.services import ingest_native
    from app.services.ingest_pdf import _ocr_jpeg_text

    parsed = pst_reader.to_parsed(item, kind)
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
        doc.metadata_ = meta
    return docs


def _read_subject_at(storage_path: str, slices, type_filter: set[str] | None) -> str:
    with pst_reader.open_pst(storage_path) as handle:
        found = _next_selected(pst_reader.iter_slice(handle, slices), type_filter)
        return _subject_of(found[2]) if found else ""


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


async def _fail_single(db, c, sel: dict, survey_json: dict | None, attempts: int) -> None:
    """Record a one-item chunk that keeps failing as a failed item."""
    from app.services.ingest import append_job_errors, incr_job_items

    folder_index, raw_pos = c.slices[0][0], c.slices[0][1]
    folder_path = _folder_path(survey_json, folder_index)
    subject = ""
    if attempts == MAX_CHUNK_ATTEMPTS + 1:
        # Try once: reading the item may be exactly what kills the worker.
        try:
            subject = await asyncio.to_thread(
                _read_subject_at, c.container_path, c.slices, set(c.type_filter) or None)
        except Exception:
            logger.exception("Could not read subject of failed item in chunk %s", c.id)
    control = message_control(sel.get("control", ""), c.global_offset + 1, int(sel.get("items") or 1))
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


async def _load_items(db, c, job) -> None:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.models import Document
    from app.services.ingest import (
        _persist_documents,
        _source_stamp,
        _stamp_source,
        append_job_errors,
        incr_job_items,
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
    # Release the connection before the long PST open/stream.
    await db.commit()

    type_filter = set(c.type_filter) or None
    cm = pst_reader.open_pst(c.container_path)
    handle = await asyncio.to_thread(cm.__enter__)
    local = 0
    try:
        gen = pst_reader.iter_slice(handle, c.slices)
        while local < c.item_count:
            found = await asyncio.to_thread(_next_selected, gen, type_filter)
            if found is None:
                break
            info, raw_pos, item, kind = found
            control = controls[local]
            local += 1
            if control in existing:
                continue
            try:
                docs = await asyncio.to_thread(
                    _build_item, item, kind, control, info.path, production_id, source_path, custodian)
                for d in docs:
                    _stamp_source(d, stamp)
                await _persist_documents(db, job_id, docs, items_done=1)
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
                await append_job_errors(db, job_id, [msg], commit=False)
                await incr_job_items(db, job_id, failed=1, commit=False)
                await db.commit()
    finally:
        await asyncio.to_thread(cm.__exit__, None, None, None)

    if local < c.item_count:
        # The slice held fewer selected items than planned: account for every
        # missing position so nothing is silently dropped.
        missing = c.item_count - local
        msg = (f"{controls[local]}..{controls[-1]}: chunk read {local} items, expected "
               f"{c.item_count}; {missing} could not be read")
        await append_job_errors(db, job_id, [msg], commit=False)
        await incr_job_items(db, job_id, failed=missing, commit=False)
        await db.commit()


async def run_chunk(db, chunk_id: str) -> None:
    """Process one ``ingest_chunks`` row (Cloud Tasks worker)."""
    attempts = (await db.execute(text(_CLAIM_CHUNK_SQL), {"id": chunk_id})).scalar()
    await db.commit()
    if attempts is None:
        return  # done, split, failed, or gone
    chunk = await db.get(IngestChunk, chunk_id, populate_existing=True)
    if chunk is None:
        return
    c = _snapshot_chunk(chunk)
    if c.kind != "items":
        logger.warning("Chunk %s has kind %r, not handled here", chunk_id, c.kind)
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
        await _fail_single(db, c, sel, survey_json, attempts)
        await maybe_finalize(db, c.job_id)
        return

    await _load_items(db, c, job)
    await db.execute(text(_CHUNK_DONE_SQL), {"id": c.id})
    await db.commit()
    await maybe_finalize(db, c.job_id)
