"""Large-container (multi-GB PST/OST) ingest: detection, survey, selection.

Containers at or above ``settings.large_container_bytes`` skip the readpst
batch path. Each is unpacked once by the ``vigilist-pst-explode`` Cloud Run
Job (``pst_explode``): downloaded, surveyed and exploded to Storage, so the
user can see counts and cost before any paid work starts and chunk workers
never open the PST. The uploader's type selection is then planned into fixed
chunks, one Cloud Task each.
"""

import asyncio
import logging
import os
import uuid

from sqlalchemy import text

from app.config import settings
from app.models import IngestChunk, IngestJob, Production
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


def start_unpack(job_id: str, storage_path: str) -> None:
    """Start the explode Job for one container (raises if it can't)."""
    from app.services import pst_explode

    pst_explode.trigger_explode_job(job_id, storage_path)


# Survey (unpack) attempts per container before it is given up on. Counted in
# the job's field_mapping; every claim counts, so a Job run that dies outright
# (OOM, timeout) also uses one up. The Job itself runs at most
# 1 + EXPLODE_JOB_MAX_RETRIES times, and its last run gives up on failure.
MAX_SURVEY_ATTEMPTS = 3

# Atomic per-container attempt counter; only while the job is surveying.
_BUMP_SURVEY_ATTEMPTS_SQL = (
    "UPDATE ingest_jobs SET field_mapping = jsonb_set(field_mapping, '{survey_attempts}', "
    "coalesce(field_mapping -> 'survey_attempts', cast('{}' as jsonb)) || jsonb_build_object("
    "cast(:path as text), coalesce(cast(field_mapping -> 'survey_attempts' ->> cast(:path as text) "
    "as integer), 0) + 1)) "
    "WHERE id = :jid AND status = 'surveying' "
    "RETURNING cast(field_mapping -> 'survey_attempts' ->> cast(:path as text) as integer)"
)


# The explode Job records progress per container in
# field_mapping["survey_progress"] (at start, then while items advance).
_SURVEY_PROGRESS_SQL = (
    "UPDATE ingest_jobs SET field_mapping = jsonb_set(field_mapping, '{survey_progress}', "
    "coalesce(field_mapping -> 'survey_progress', cast('{}' as jsonb)) || jsonb_build_object("
    "cast(:path as text), cast(:at as text))) "
    "WHERE id = :jid AND status = 'surveying'"
)
# A container whose unpack has shown no progress this long is given up on by
# the status endpoint's self-heal (the Job's task timeout is 4 h).
SURVEY_STALL_S = 4.5 * 3600
SURVEY_STALL_LABEL = "no progress for 4.5 h"


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _as_utc(value):
    from datetime import datetime, timezone

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def record_survey_progress(db, job_id: str, storage_path: str) -> None:
    await db.execute(text(_SURVEY_PROGRESS_SQL), {
        "jid": job_id, "path": storage_path, "at": _utcnow().isoformat()})
    await db.commit()


def stalled_survey_paths(job, now) -> list[str]:
    """Large containers neither surveyed nor given up on whose last unpack
    progress (else the job's creation) is older than SURVEY_STALL_S."""
    fm = job.field_mapping or {}
    progress = fm.get("survey_progress") or {}
    failed = fm.get("survey_failed") or {}
    surveys = job.survey or {}
    created = _as_utc(getattr(job, "created_at", None))
    out = []
    for path in fm.get("large_containers") or []:
        if path in surveys or path in failed:
            continue
        last = _as_utc(progress.get(path)) or created
        if last is not None and (now - last).total_seconds() > SURVEY_STALL_S:
            out.append(path)
    return out


async def give_up_stalled_surveys(db, job_id: str) -> None:
    """Self-heal: give up containers whose unpack Job stopped making progress
    (it died on its last run, or its execution never started)."""
    job = await db.get(IngestJob, job_id, populate_existing=True)
    if job is None or job.status != "surveying":
        return
    paths = stalled_survey_paths(job, _utcnow())
    await db.commit()
    for path in paths:
        logger.warning("Job %s: unpack of %s made no progress for 4.5 h; giving up", job_id, path)
        await _give_up_survey(
            db, job_id, path, 0, SURVEY_STALL_LABEL,
            line=f"{os.path.basename(path)}: mailbox unpack did not finish ({SURVEY_STALL_LABEL})")


def survey_failed_paths(job) -> dict:
    """``{storage_path: short error}`` for large containers whose survey was
    given up on."""
    return dict((job.field_mapping or {}).get("survey_failed") or {})


def _short_error(e: BaseException) -> str:
    text_ = " ".join(f"{type(e).__name__}: {e}".split())
    return text_[:300]


def _now_naive():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


async def run_survey(db, job_id: str, storage_path: str, work, *,
                     give_up_on_failure: bool = False) -> str:
    """Survey (and, in the explode Job, unpack) one container and record it.

    ``work(storage_path) -> survey JSON`` is the blocking walk; the explode
    Job passes ``pst_explode.unpack_container``, so the survey is recorded
    only once every item is exploded. When every large container in the job
    has a survey (or was given up on), the job moves to ``awaiting_selection``.

    A failure re-raises (the caller retries) until ``MAX_SURVEY_ATTEMPTS``,
    or at once with ``give_up_on_failure`` (the Job's last run); then the
    container is given up on (``_give_up_survey``) and this returns.

    A ``PermanentSurveyFailure`` (e.g. too large to unpack) gives up at once.

    Returns ``"surveyed"``, ``"settled"`` (already surveyed or given up),
    ``"not_surveying"``, ``"gave_up"``, ``"refused"`` (permanent failure) or
    ``"discarded"`` (given up on, e.g. by the stall self-heal, while this
    run was still working; its result is not recorded).
    """
    from app.services.pst_explode import PermanentSurveyFailure

    job = await db.get(IngestJob, job_id)
    if not job:
        return "not_surveying"
    if storage_path in (job.survey or {}) or storage_path in survey_failed_paths(job):
        await db.commit()
        return "settled"  # a rerun for a container already settled
    attempts = (await db.execute(text(_BUMP_SURVEY_ATTEMPTS_SQL),
                                 {"jid": job_id, "path": storage_path})).scalar()
    # Commits the counter and releases the connection for the (minutes-long)
    # container walk.
    await db.commit()
    if attempts is None:
        return "not_surveying"
    if attempts > MAX_SURVEY_ATTEMPTS:
        # The previous attempts never got to record a failure: the run died.
        await _give_up_survey(db, job_id, storage_path, attempts - 1,
                              "the worker stopped before the survey finished")
        return "gave_up"

    try:
        survey_json = await asyncio.to_thread(work, storage_path)
    except PermanentSurveyFailure as e:
        logger.warning("Container %s refused: %s", storage_path, e.line)
        await _give_up_survey(db, job_id, storage_path, attempts, e.line, line=e.line)
        return "refused"
    except Exception as e:
        if attempts < MAX_SURVEY_ATTEMPTS and not give_up_on_failure:
            raise
        logger.exception("Survey of %s failed (attempt %d); giving up", storage_path, attempts)
        await _give_up_survey(db, job_id, storage_path, attempts, _short_error(e))
        return "gave_up"

    # Re-read under a row lock so two surveys finishing together can't lose
    # each other's write or both miss the "all surveyed" transition.
    # Sessions use expire_on_commit=False, so without populate_existing the
    # identity map would hand back the stale pre-survey object.
    job = await db.get(IngestJob, job_id, with_for_update=True, populate_existing=True)
    if storage_path in survey_failed_paths(job):
        await db.rollback()
        return "discarded"
    surveys = dict(job.survey or {})
    surveys[storage_path] = survey_json
    job.survey = surveys
    expected = (job.field_mapping or {}).get("large_containers") or []
    failed = survey_failed_paths(job)
    if all(p in surveys or p in failed for p in expected) and job.status == "surveying":
        job.status = "awaiting_selection"
    await db.commit()
    return "surveyed"


async def _give_up_survey(db, job_id: str, storage_path: str, attempts: int, error: str,
                          *, reason: str | None = None, line: str | None = None) -> None:
    """Record a container that could not be surveyed and let the job go on
    without it.

    Appends an error line, counts the container's source key as skipped and
    records it in ``field_mapping["survey_failed"]`` (finalize reports it).
    Once every large container is surveyed or given up on: with at least one
    surveyed, the job awaits selection as usual; with none surveyed but other
    files in the load, it continues to finalize; with nothing else at all, the
    job is ``failed``. Whatever the explode Job wrote for the container (or,
    when the job failed, for the whole job) is deleted.
    """
    from app.services.ingest import _INCR_SKIPPED_SQL, append_job_errors

    job = await db.get(IngestJob, job_id, with_for_update=True, populate_existing=True)
    if job is None or job.status != "surveying":
        await db.rollback()
        return
    fm = dict(job.field_mapping or {})
    failed = dict(fm.get("survey_failed") or {})
    surveys = job.survey or {}
    if storage_path in failed or storage_path in surveys:
        await db.rollback()
        return
    failed[storage_path] = error
    fm["survey_failed"] = failed
    job.field_mapping = fm

    expected = fm.get("large_containers") or []
    finalize = False
    if all(p in surveys or p in failed for p in expected):
        if any(p in surveys for p in expected):
            job.status = "awaiting_selection"
        elif int(job.total_files or 0) > len(expected):
            # Only normal files remain: finalize once they are attempted.
            job.status = "processing"
            finalize = True
        else:
            job.status = "failed"
            job.completed_at = _now_naive()

    name = os.path.basename(storage_path)
    msg = line or (f"{name}: mailbox could not be read "
                   f"({reason or f'survey failed {attempts} times'}): {error}")
    job_failed = job.status == "failed"
    production_id = getattr(job, "production_id", None)
    await append_job_errors(db, job_id, [msg], commit=False)
    await db.execute(text(_INCR_SKIPPED_SQL), {"key": large_source_key(storage_path), "jid": job_id})
    # Failure record, error line, skipped key and status commit together.
    await db.commit()
    logger.warning("Job %s: gave up surveying %s after %d attempts", job_id, storage_path, attempts)
    if production_id is not None:
        from app.services import pst_explode

        prefixes = [pst_explode.explode_prefix(production_id, str(job_id), storage_path)]
        if job_failed:
            prefixes.append(pst_explode.job_explode_prefix(production_id, str(job_id)))
        for prefix in prefixes:
            await asyncio.to_thread(pst_explode.delete_exploded, prefix)
    if finalize:
        # Normal batches that already finished saw a 'surveying' job and did
        # not finalize; nudge it now.
        await maybe_finalize(db, job_id)


# Selected items per chunk. Provisional until the U1 throughput spike sizes it
# to finish well inside the 30-minute dispatch deadline.
CHUNK_ITEMS = 500


async def maybe_finalize(db, job_id: str) -> None:
    """Advance the job's finalize if it is ready; see ``large_ingest_finalize``."""
    from app.services import large_ingest_finalize

    await large_ingest_finalize.maybe_finalize(db, job_id)


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


# Earlier jobs' selections for the same container in this production, newest
# first (the selection stores each container's control number).
_PRIOR_SELECTIONS_SQL = (
    "SELECT ingest_jobs.selection -> cast(:path as text) FROM ingest_jobs "
    "WHERE production_id = :pid AND id <> cast(:jid as uuid) "
    "AND ingest_jobs.selection -> cast(:path as text) ->> 'control' IS NOT NULL "
    "ORDER BY created_at DESC"
)


def _matching_prior(priors, types: set[str], include_recoverable: bool, items: int) -> dict | None:
    """The newest earlier selection of this container that planned the same
    items. Message positions (and so controls) depend on the selected types,
    the recoverable choice and the item count; reusing a control under any
    other selection would skip the wrong messages."""
    for prior in priors:
        if (isinstance(prior, dict) and prior.get("control")
                and sorted(prior.get("types") or []) == sorted(types)
                and bool(prior.get("include_recoverable", True)) == bool(include_recoverable)
                and int(prior.get("items") or -1) == items):
            return prior
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
    priors = [r[0] for r in (await db.execute(text(_PRIOR_SELECTIONS_SQL), {
        "path": storage_path, "pid": production_id, "jid": str(job_id),
    })).all()]
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
        await db.rollback()  # release the row lock
        raise SelectionError(409, "Selection already applied for this container")
    if job.status != "awaiting_selection":
        await db.rollback()
        raise SelectionError(409, f"Job is {job.status}, not awaiting selection")

    type_set = set(types)
    survey = ContainerSurvey.from_json(job.survey[storage_path])
    try:
        plans = plan_chunks(survey, type_set, include_recoverable, CHUNK_ITEMS)
    except ValueError as e:
        await db.rollback()
        raise SelectionError(409, f"{e}") from e
    estimate = estimate_load(survey, type_set, include_recoverable)
    selected = sum(p["item_count"] for p in plans)

    # Re-ingesting a container an earlier job already loaded (same selection):
    # reuse that job's control so the per-message skip turns this run into a
    # resume instead of duplicating every message.
    prior = _matching_prior(priors, type_set, include_recoverable, selected)
    already = 0
    if prior is not None:
        from app.services.large_ingest_finalize import _LOADED_MESSAGES_SQL, _like_escape

        control = prior["control"]
        already = int((await db.execute(text(_LOADED_MESSAGES_SQL), {
            "pid": production_id, "pat": f"{_like_escape(control)} -%",
        })).scalar() or 0)
        logger.info("Job %s: %s was loaded before as %s (%d messages); resuming",
                    job_id, storage_path, control, already)

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
    if already:
        # Read under the row lock, so concurrent atomic increments are kept.
        job.items_done = int(job.items_done or 0) + min(already, selected)
    failed = survey_failed_paths(job)
    if all(p in selection or p in failed for p in fm.get("large_containers") or []):
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
