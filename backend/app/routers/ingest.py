"""Ingest endpoints: start processing, process batch, check status."""

import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
import re

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.models import Document, IngestJob, Production, User
from app.routers.auth import get_current_user
from app.schemas import AnalyzeResponse, IngestJobOut
from app.services.audit import log_action
from app.services.oidc import verify_cloud_tasks_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ingest"])


@router.post("/ingest/create")
async def create_production_for_ingest(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Phase 1: Create the production record and sync Firebase claims.

    The frontend calls this FIRST to get the production_id, then uploads
    files to Firebase Storage under productions/{production_id}/raw/,
    then calls /ingest/process to start backend processing.
    """
    production_name = body.get("production_name", "").strip()
    description = body.get("description", "").strip()

    if not production_name:
        raise HTTPException(status_code=400, detail="production_name is required")

    existing = await db.execute(
        select(Production).where(Production.name == production_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Production with this name already exists")

    # File the production under the creator's organization, if any, so every
    # member of that org can access it.
    from app.dependencies import resolve_org_for_creator
    org_id = await resolve_org_for_creator(db, user)

    production = Production(
        name=production_name,
        description=description,
        case_context=(body.get("case_context") or "").strip() or None,
        owner_id=user.id,
        organization_id=org_id,
    )
    db.add(production)
    await db.commit()
    await db.refresh(production)

    # Sync Firebase claims so user can upload to this production's storage path
    from app.services.claims import sync_user_claims
    await sync_user_claims(db, user)

    return {"production_id": production.id, "production_name": production.name}


_LOAD_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")


def _load_prefix_from(body: dict) -> str | None:
    """'loads/{load_id}/' storage namespace for this load, or None (legacy)."""
    load_id = body.get("load_id") or None
    if load_id is None:
        return None
    if not _LOAD_ID_RE.fullmatch(str(load_id)):
        raise HTTPException(status_code=422, detail="invalid load_id")
    return f"loads/{load_id}/"


@router.post("/ingest/analyze", response_model=AnalyzeResponse)
async def analyze_ingest(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Parse the uploaded load file and return a proposed column mapping.

    Downloads the production's DAT file from Firebase Storage, detects encoding
    and delimiter, parses headers + sample rows, and runs alias + AI mapping.
    Returns format metadata, proposed column mappings, and sample rows so the
    frontend can render a field-mapping UI before starting ingest.
    """
    from app.services.ingest import analyze_load_file

    production_id = body.get("production_id")
    if not production_id:
        raise HTTPException(status_code=400, detail="production_id is required")
    production = await db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=404, detail="Production not found")
    if production.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    load_prefix = _load_prefix_from(body)
    try:
        return await run_in_threadpool(analyze_load_file, int(production_id), load_prefix)
    except Exception as e:
        logger.exception("Analyze failed for production %s", production_id)
        raise HTTPException(status_code=400, detail=f"Could not analyze load file: {e}")


@router.post("/ingest/process", response_model=IngestJobOut)
async def start_processing(
    body: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Phase 2: Start processing after the frontend upload is complete.

    If Cloud Tasks is configured, parses the DAT/OPT files, creates an
    IngestJob with the correct total_files count, and enqueues one
    Cloud Task per batch. Each task runs in its own Cloud Run request
    so long ingests can't be killed by container scale-down.

    Falls back to an inline FastAPI BackgroundTask when Cloud Tasks
    isn't configured — fine for local dev, unreliable on Cloud Run
    for long jobs.
    """
    from app.services import tasks as task_service
    from app.services.ingest import (
        INGEST_BATCH_SIZE,
        bootstrap_ingest_source,
        ingest_from_storage,
    )

    production_id = body.get("production_id")
    if not production_id:
        raise HTTPException(status_code=400, detail="production_id is required")

    production = await db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=404, detail="Production not found")
    if production.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    source_format = body.get("source_format", "relativity")
    field_mapping = body.get("field_mapping") or {}
    if source_format == "native":
        field_mapping = {"custodian": (body.get("custodian") or "").strip() or None}
    source_party = (body.get("source_party") or "").strip() or None
    source_type = body.get("source_type") or None
    if source_type not in (None, "collection", "received"):
        raise HTTPException(status_code=422, detail="source_type must be 'collection' or 'received'")
    field_mapping = {**field_mapping, "source_party": source_party, "source_type": source_type}
    load_prefix = _load_prefix_from(body)
    if load_prefix:
        field_mapping["load_prefix"] = load_prefix
    if source_format in ("generic_pdf", "native"):
        # Continue the '{PREFIX} NNNNNN' control sequence across loads so a
        # second load can't collide with the bates unique key and be skipped.
        from app.services.ingest import compute_control_offset
        from app.services.ingest_pdf import derive_bates_prefix

        control_prefix = derive_bates_prefix(production.name)
        bates_rows = (await db.execute(
            select(Document.bates_begin).where(
                Document.production_id == production.id,
                Document.bates_begin.like(f"{control_prefix} %"),
            )
        )).all()
        offset = compute_control_offset([r[0] for r in bates_rows], control_prefix)
        if offset:
            field_mapping["control_offset"] = offset
    batch_size = 10 if source_format in ("generic_pdf", "native") else INGEST_BATCH_SIZE

    large_containers: list[str] = []
    if task_service.is_configured():
        # Count source items to set an accurate total_files, then enqueue tasks
        try:
            if source_format == "generic_pdf":
                from app.services.ingest_pdf import list_pdf_sources
                total_files = len(list_pdf_sources(production.id, load_prefix))
            elif source_format == "native":
                from app.services.ingest_native import list_native_sources
                from app.services.large_ingest import is_large_container
                native_items = list_native_sources(production.id, load_prefix)
                total_files = len(native_items)
                large_containers = [
                    it["storage_path"] for it in native_items if is_large_container(it)
                ]
            else:
                records, _ = bootstrap_ingest_source(production.id, load_prefix)
                total_files = len(records)
        except Exception as e:
            logger.exception("Failed to parse ingest source")
            raise HTTPException(status_code=400, detail=f"Failed to parse production files: {e}")

        if total_files == 0:
            raise HTTPException(status_code=400, detail="No ingestable files found in upload")

        if large_containers:
            field_mapping = {**field_mapping, "large_containers": large_containers}
        job = IngestJob(
            production_id=production.id,
            user_id=user.id,
            # Large containers are surveyed before any paid work; the normal
            # batches for the other files still run in parallel.
            status="surveying" if large_containers else "processing",
            source_format=source_format,
            total_files=total_files,
            field_mapping=field_mapping,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        enqueued = 0
        enqueue_errors: list[str] = []
        for start in range(0, total_files, batch_size):
            end = start + batch_size
            try:
                task_service.enqueue_ingest_batch(
                    job_id=str(job.id),
                    production_id=production.id,
                    start_idx=start,
                    end_idx=end,
                )
                enqueued += 1
            except Exception as e:
                logger.exception("Failed to enqueue batch %d-%d", start, end)
                enqueue_errors.append(f"Enqueue failed for batch {start}-{end}: {e}")

        for path in large_containers:
            try:
                task_service.enqueue_survey(str(job.id), path)
            except Exception as e:
                logger.exception("Failed to enqueue survey for %s", path)
                enqueue_errors.append(f"Enqueue failed for survey of {path}: {e}")

        if enqueue_errors:
            job.errors = enqueue_errors
            await db.commit()

        logger.info("Enqueued %d batches for job %s", enqueued, job.id)

        return IngestJobOut(
            id=job.id,
            production_id=production.id,
            production_name=production.name,
            status=job.status,
            total_files=job.total_files,
            processed_files=0,
            errors=job.errors or [],
            created_at=job.created_at,
            completed_at=None,
        )

    # Fallback: inline BackgroundTask
    total_files = body.get("total_files", 0)
    job = IngestJob(
        production_id=production.id,
        user_id=user.id,
        status="processing",
        source_format=source_format,
        total_files=total_files,
        field_mapping=field_mapping,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    if source_format == "native":
        await _skip_large_containers_inline(db, job, production.id, load_prefix)

    background_tasks.add_task(
        run_ingest_job,
        job_id=str(job.id),
        production_id=production.id,
        production_name=production.name,
    )

    return IngestJobOut(
        id=job.id,
        production_id=production.id,
        production_name=production.name,
        status=job.status,
        total_files=job.total_files,
        processed_files=0,
        errors=[],
        created_at=job.created_at,
        completed_at=None,
    )


async def _skip_large_containers_inline(db, job, production_id: int, load_prefix) -> None:
    """Inline mode has no worker for the chunked path: report each large
    mailbox as an error and count it skipped so the job can still finish."""
    from app.services.ingest import _incr_skipped
    from app.services.ingest_native import list_native_sources
    from app.services.large_ingest import is_large_container, large_source_key

    try:
        large = [it for it in list_native_sources(production_id, load_prefix) if is_large_container(it)]
    except Exception:
        logger.exception("Could not list sources to check for large containers")
        return
    if not large:
        return
    job.errors = list(job.errors or []) + [
        f"{it['filename']}: large mailbox loading requires Cloud Tasks" for it in large
    ]
    await db.commit()
    for it in large:
        await _incr_skipped(db, str(job.id), large_source_key(it["storage_path"]))


# A Cloud Tasks request that fails before its chunk/stage claim (e.g. the
# database is unavailable) has no persisted attempt counter, so the task's
# own retry count caps it: past this many attempts it is logged (and recorded
# on the job where one can be found) and acknowledged with 200, instead of
# crash-looping for days under the queue's maxAttempts 100. A dropped chunk
# task is re-sent by finalize's orphan sweep once the chunk sits pending.
PRE_CLAIM_MAX_ATTEMPTS = 3
_RETRY_HEADER = "X-CloudTasks-TaskRetryCount"


def _task_attempt(retry_count) -> int:
    """1-based attempt number from the X-CloudTasks-TaskRetryCount header."""
    try:
        return int(retry_count) + 1
    except (TypeError, ValueError):
        return 1


async def _record_chunk_claim_failure(db, chunk_id: str, attempt: int, error: str) -> None:
    """Best-effort: append the dropped claim to the chunk's job errors."""
    from app.models import IngestChunk
    from app.services.ingest import append_job_errors

    try:
        await db.rollback()
        chunk = await db.get(IngestChunk, chunk_id)
        if chunk is not None:
            await append_job_errors(db, str(chunk.job_id), [
                f"Chunk {chunk_id}: could not be started after {attempt} attempts: {error[:300]}"])
    except Exception:
        logger.exception("Could not record the claim failure of chunk %s", chunk_id)


@router.post("/ingest/process-batch")
async def process_batch_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker endpoint — processes one batch of ingest records.

    Protected by OIDC token verification. Cloud Tasks will retry on
    non-2xx responses, so the batch processor is idempotent.
    """
    from app.services.ingest import run_ingest_batch

    job_id = body.get("job_id")
    production_id = body.get("production_id")
    start_idx = body.get("start_idx")
    end_idx = body.get("end_idx")

    if job_id is None or production_id is None or start_idx is None or end_idx is None:
        raise HTTPException(status_code=400, detail="Missing required fields")

    try:
        await run_ingest_batch(db, job_id, int(production_id), int(start_idx), int(end_idx))
    except Exception as e:
        logger.exception("Ingest batch failed")
        raise HTTPException(status_code=500, detail=f"Ingest batch failed: {e}")

    return {"ok": True}


@router.post("/ingest/survey-container")
async def survey_container_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker: survey one large mailbox container (read-only).

    ``run_survey`` counts attempts on the job and gives the container up after
    ``MAX_SURVEY_ATTEMPTS`` (returning normally, so this answers 200 and Cloud
    Tasks stops); earlier failures answer 500 so Cloud Tasks retries.
    """
    from app.services.large_ingest import run_survey

    job_id = body.get("job_id")
    storage_path = body.get("storage_path")
    if not job_id or not storage_path:
        raise HTTPException(status_code=400, detail="Missing required fields")
    try:
        await run_survey(db, job_id, storage_path)
    except Exception as e:
        logger.exception("Container survey failed")
        raise HTTPException(status_code=500, detail=f"Container survey failed: {e}")
    return {"ok": True}


@router.post("/ingest/process-chunk")
async def process_chunk_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
    retry_count: str | None = Header(None, alias=_RETRY_HEADER),
):
    """Cloud Tasks worker: process one large-container chunk of any kind
    (``items`` load, ``embed`` search embeddings, ``post`` threading/titles and
    completion); ``run_chunk`` dispatches on the row's kind.

    Non-2xx makes Cloud Tasks retry; the chunk's attempt counter splits a range
    that keeps failing, so retries can't crash-loop forever.
    """
    from app.services import large_ingest_chunk

    chunk_id = body.get("chunk_id")
    if not chunk_id:
        raise HTTPException(status_code=400, detail="Missing required fields")
    try:
        await large_ingest_chunk.run_chunk(db, str(chunk_id))
    except large_ingest_chunk.ChunkClaimError as e:
        attempt = _task_attempt(retry_count)
        if attempt < PRE_CLAIM_MAX_ATTEMPTS:
            logger.exception("Ingest chunk %s could not be claimed (attempt %d)", chunk_id, attempt)
            raise HTTPException(status_code=500, detail=f"Ingest chunk failed: {e}")
        logger.error("Ingest chunk %s could not be claimed after %d attempts; dropping the task: %s",
                     chunk_id, attempt, e)
        await _record_chunk_claim_failure(db, str(chunk_id), attempt, str(e))
        return {"ok": False, "dropped": True}
    except large_ingest_chunk.ChunkBusy as e:
        # Another live attempt holds the chunk: Cloud Tasks backs off and
        # retries; the claim succeeds once that attempt is done or stale.
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("Ingest chunk %s failed", chunk_id)
        raise HTTPException(status_code=500, detail=f"Ingest chunk failed: {e}")
    return {"ok": True}


@router.post("/ingest/{job_id}/selection")
async def apply_container_selection(
    job_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The uploader's item-type choice for one surveyed large container.

    Body: ``{"storage_path": str, "types": [item types], "include_recoverable": bool}``.
    Plans the selected items into chunks and starts loading them.
    """
    from app.services.large_ingest import SelectionError, apply_selection
    from app.services.pst_reader import ITEM_TYPES

    job = await db.get(IngestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    storage_path = body.get("storage_path")
    types = body.get("types")
    include_recoverable = body.get("include_recoverable", True)
    if not isinstance(storage_path, str) or not storage_path:
        raise HTTPException(status_code=422, detail="storage_path is required")
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        raise HTTPException(status_code=422, detail="types must be a list of item types")
    unknown = sorted(set(types) - set(ITEM_TYPES))
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown item types: {', '.join(unknown)}")
    if not isinstance(include_recoverable, bool):
        raise HTTPException(status_code=422, detail="include_recoverable must be a boolean")

    try:
        estimate = await apply_selection(db, str(job_id), storage_path, types, include_recoverable)
    except SelectionError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return {"job_id": str(job_id), "storage_path": storage_path, "estimate": estimate}


@router.post("/ingest/run-pipeline")
async def run_pipeline_handler(
    body: dict,
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker: run the ambient AI pipeline for one production."""
    production_id = body.get("production_id")
    if not production_id:
        raise HTTPException(status_code=400, detail="production_id required")
    force = bool(body.get("force"))

    # A named job runs on its own instead of walking STAGES. Standalone work
    # (the merge review) is not a pipeline stage — it must not run on every
    # upload — so without this branch it would be enqueued and never executed.
    job = body.get("job")
    if job == "merge_review":
        from app.services.pipeline import run_merge_review_stage
        await run_merge_review_stage(int(production_id))
        return {"ok": True, "job": job}
    if job:
        raise HTTPException(status_code=400, detail=f"Unknown job {job!r}")

    from app.services.pipeline import run_ambient_pipeline
    await run_ambient_pipeline(int(production_id), force)
    return {"ok": True}


@router.post("/ingest/poll-batch")
async def poll_batch_handler(
    body: dict,
    _verified: None = Depends(verify_cloud_tasks_request),
    retry_count: str | None = Header(None, alias=_RETRY_HEADER),
):
    """Cloud Tasks worker: poll one pipeline stage's Message Batch; apply the
    results and continue the pipeline once it has ended."""
    from app.services import pipeline

    production_id = body.get("production_id")
    stage = body.get("stage")
    if not production_id or stage not in pipeline.BATCHED_STAGES:
        raise HTTPException(status_code=400, detail="production_id and a batched stage required")
    try:
        await pipeline.poll_batch_stage(int(production_id), stage)
    except pipeline.BatchPollClaimError as e:
        attempt = _task_attempt(retry_count)
        if attempt < PRE_CLAIM_MAX_ATTEMPTS:
            logger.exception("Batch poll for production %s %s failed (attempt %d)",
                             production_id, stage, attempt)
            raise HTTPException(status_code=500, detail=str(e))
        # No ingest job here. The stage stays 'waiting': once it is older than
        # BATCH_STALE_HOURS the next pipeline run resubmits it.
        logger.error("Batch poll for production %s %s failed %d times; dropping the task: %s",
                     production_id, stage, attempt, e)
        return {"ok": False, "dropped": True}
    return {"ok": True}


@router.post("/productions/{production_id}/extract-entities")
async def trigger_entity_extraction(
    production_id: int,
    background_tasks: BackgroundTasks,
    rebuild: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Backfill/manual trigger: run the ambient pipeline (which now includes
    the entities stage) for this production. Manager or admin only; 404 for
    productions outside the caller's scope.

    With rebuild=true this is THE destructive re-extraction path (the old
    /reset-entities endpoint was consolidated into it): the production's
    entire ontology (entities, mentions, events, participants, relationships,
    merge suggestions) is deleted, every document's extraction mark is
    cleared, and the pipeline's entities/timeline_review/brief stage state is
    reset, so the run re-reads the whole matter — the recovery path after
    extraction-quality changes.
    """
    from app.dependencies import (ROLE_RANK, get_accessible_production_ids,
                                  get_user_role_for_production)
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    prod = await db.get(Production, production_id)
    if prod is not None:
        # An explicit run clears the sticky AI hold (committed below, before
        # the enqueue).
        prod.ai_hold = False

    if rebuild:
        from sqlalchemy import delete, update
        from app.models import (Entity, EntityMention, EntityMergeSuggestion,
                                EntityRelationship, EventParticipant, OntologyEvent)
        # Participants first (they reference both events and entities), then
        # events — both cascade from productions, NOT entities, so deleting
        # entities alone would leave them behind. Then the remaining entity
        # referents, then the entities themselves; the explicit deletes make
        # the entity FK cascades belt-and-braces rather than load-bearing.
        # entity_merges audit rows reference entities with SET NULL and are
        # intentionally preserved.
        await db.execute(delete(EventParticipant).where(
            EventParticipant.event_id.in_(
                select(OntologyEvent.id).where(OntologyEvent.production_id == production_id))))
        await db.execute(delete(OntologyEvent).where(OntologyEvent.production_id == production_id))
        await db.execute(delete(EntityMention).where(EntityMention.production_id == production_id))
        await db.execute(delete(EntityRelationship).where(EntityRelationship.production_id == production_id))
        await db.execute(delete(EntityMergeSuggestion).where(EntityMergeSuggestion.production_id == production_id))
        await db.execute(delete(Entity).where(Entity.production_id == production_id))
        # Clear the per-document extraction watermark so the entities stage
        # re-reads every document.
        await db.execute(update(Document).where(Document.production_id == production_id)
                         .values(entities_extracted_at=None))

        # Also clear the pipeline's stage state, or the re-run is a no-op:
        # stages_to_run() skips any stage marked "done", and an
        # already-processed matter has entities: "done" — leaving that in
        # place after wiping the ontology would strand the matter permanently
        # empty. We drop the entities + timeline_review + brief keys (both
        # are derived from entities, so both must regenerate too) rather than
        # enqueueing with force=True, which would needlessly re-run
        # clustering and summaries whose outputs the rebuild leaves intact.
        if prod is not None:
            status = dict(prod.ai_pipeline_status or {})
            errors = dict(status.get("errors") or {})
            for stage in ("entities", "timeline_review", "brief"):
                status.pop(stage, None)
                errors.pop(stage, None)
            status["errors"] = errors
            prod.ai_pipeline_status = status  # reassign: in-place JSONB mutation isn't tracked

    from app.services import tasks as task_service
    mode = "enqueued" if task_service.is_configured() else "background"
    await log_action(db, user, "entity_extraction_triggered", "production", str(production_id),
                     production_id=production_id, details={"mode": mode, "rebuild": rebuild})
    # Commit BEFORE handing off to the worker: it must see the cleared
    # entities_extracted_at watermarks and stage state durably, or it can
    # read pre-delete rows and skip everything (stale-watermark race).
    await db.commit()
    if mode == "enqueued":
        task_service.enqueue_pipeline(production_id)
        return {"status": "enqueued"}
    from app.services.pipeline import run_ambient_pipeline
    background_tasks.add_task(run_ambient_pipeline, production_id)
    return {"status": "started"}


# The held estimate sums text lengths over every pending document, so it is
# cached per production per instance (in-process, like the other throttles).
AI_ESTIMATE_CACHE_S = 600
# Throttle/cache entries older than this are pruned whenever one is written.
THROTTLE_PRUNE_S = 3600
_AI_ESTIMATE_CACHE: dict[int, tuple[float, dict]] = {}


def _monotonic() -> float:
    import time

    return time.monotonic()


def _prune(cache: dict, now: float) -> None:
    """Drop entries written more than THROTTLE_PRUNE_S ago. Values are a
    timestamp or a (timestamp, payload) tuple."""
    for key, value in list(cache.items()):
        at = value[0] if isinstance(value, tuple) else value
        if now - at > THROTTLE_PRUNE_S:
            cache.pop(key, None)


@router.get("/productions/{production_id}/ai-estimate")
async def ai_estimate(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Whether the production's AI is on hold, how many documents still need
    per-document AI (a summary or entity extraction), and what running it
    would cost at Haiku 5.5 batch pricing. Shown before any paid work."""
    from sqlalchemy import case, func

    from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
    from app.services.ai import SUMMARY_INPUT_CHARS, estimate_per_doc_usd

    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    prod = await db.get(Production, production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="Production not found")
    # Same gate as POST /pipeline/run: only managers/admins can start analysis.
    role = await get_user_role_for_production(db, user, production_id)
    can_run = ROLE_RANK.get(role, 0) >= ROLE_RANK["manager"]
    if not prod.ai_hold:
        # Nothing is waiting on a decision: skip the text scan.
        return {"held": False, "documents_pending": 0, "usd_estimate": 0, "can_run": can_run}

    now = _monotonic()
    cached = _AI_ESTIMATE_CACHE.get(production_id)
    if cached is not None and now - cached[0] < AI_ESTIMATE_CACHE_S:
        return {"held": True, **cached[1], "can_run": can_run}

    needs_summary = Document.summary.is_(None)
    needs_entities = Document.entities_extracted_at.is_(None)
    length = func.char_length(Document.text_content)
    row = (await db.execute(
        select(
            func.count(),
            func.count(case((needs_summary, 1))),
            func.coalesce(func.sum(case((needs_summary, func.least(length, SUMMARY_INPUT_CHARS)),
                                        else_=0)), 0),
            func.count(case((needs_entities, 1))),
            func.coalesce(func.sum(case((needs_entities, length), else_=0)), 0),
        ).where(
            Document.production_id == production_id,
            Document.text_content.isnot(None),
            needs_summary | needs_entities,
        )
    )).one()
    pending, summary_docs, summary_chars, entity_docs, entity_chars = (int(v or 0) for v in row)
    usd = estimate_per_doc_usd(summary_docs, summary_chars, entity_docs, entity_chars)
    result = {"documents_pending": pending, "usd_estimate": round(usd, 4)}
    _prune(_AI_ESTIMATE_CACHE, now)
    _AI_ESTIMATE_CACHE[production_id] = (now, result)
    return {"held": True, **result, "can_run": can_run}


# Self-heal: a processing/finalizing job whose chunks have been quiet this long
# may be stuck waiting for a finalize nobody triggered (e.g. the last chunk's
# own trigger was lost). Polling re-drives it, at most once per interval/job.
SELF_HEAL_QUIET_MINUTES = 5
SELF_HEAL_INTERVAL_S = SELF_HEAL_QUIET_MINUTES * 60
_SELF_HEAL_AT: dict[str, float] = {}
# The quiet check itself runs at most this often per job per instance (the
# status endpoint is polled every ~2 s).
QUIET_CHECK_INTERVAL_S = 30
_QUIET_CHECKED_AT: dict[str, float] = {}

_QUIET_SQL = text(
    "SELECT coalesce(max(updated_at) < now() - make_interval(mins => :mins), true) "
    "FROM ingest_chunks WHERE job_id = :job_id"
)


async def _self_heal(job_id: str) -> None:
    """Background: advance a quiet job's finalize. Never raises."""
    try:
        from app.database import async_session_factory
        from app.services import large_ingest

        async with async_session_factory() as db:
            await large_ingest.maybe_finalize(db, job_id)
    except Exception:
        logger.exception("Self-heal finalize failed for ingest job %s", job_id)


async def _maybe_schedule_self_heal(db, job, background_tasks: BackgroundTasks) -> None:
    import time

    if job.status not in ("processing", "finalizing"):
        return
    if not (job.field_mapping or {}).get("large_containers"):
        return
    key = str(job.id)
    now = time.monotonic()
    last = _SELF_HEAL_AT.get(key)
    if last is not None and now - last < SELF_HEAL_INTERVAL_S:
        return
    checked = _QUIET_CHECKED_AT.get(key)
    if checked is not None and now - checked < QUIET_CHECK_INTERVAL_S:
        return
    _prune(_QUIET_CHECKED_AT, now)
    _QUIET_CHECKED_AT[key] = now
    try:
        quiet = (await db.execute(
            _QUIET_SQL, {"mins": SELF_HEAL_QUIET_MINUTES, "job_id": job.id})).scalar()
    except Exception:
        logger.exception("Self-heal activity check failed for ingest job %s", key)
        return
    if not quiet:
        return
    _prune(_SELF_HEAL_AT, now)
    _SELF_HEAL_AT[key] = now
    background_tasks.add_task(_self_heal, key)


def _container_outs(job) -> list:
    from app.schemas import ContainerOut, ContainerRecoverableOut, ContainerTypeOut
    from app.services.large_ingest_estimate import estimate_load
    from app.services.pst_reader import ITEM_TYPES, ContainerSurvey

    fm = job.field_mapping or {}
    survey_by_path = job.survey or {}
    selection = job.selection or {}
    outs = []
    for path in fm.get("large_containers") or []:
        raw = survey_by_path.get(path)
        if not raw:
            continue  # not surveyed yet
        survey = ContainerSurvey.from_json(raw)
        normal = {t: [0, 0] for t in ITEM_TYPES}
        recov = {t: [0, 0] for t in ITEM_TYPES}
        for f in survey.folders:
            bucket = recov if f.recoverable else normal
            for t, n in f.counts.items():
                bucket.setdefault(t, [0, 0])[0] += int(n)
            for t, n in f.bytes.items():
                bucket.setdefault(t, [0, 0])[1] += int(n)
        types = [ContainerTypeOut(type=t, count=c, bytes=b) for t, (c, b) in normal.items() if c or b]
        rtypes = [ContainerTypeOut(type=t, count=c, bytes=b) for t, (c, b) in recov.items() if c or b]
        chosen = selection.get(path)
        estimate = (chosen or {}).get("estimate") or estimate_load(survey, {"email", "calendar"}, True)
        outs.append(ContainerOut(
            storage_path=path,
            name=path.rsplit("/", 1)[-1],
            size_bytes=sum(x.bytes for x in types) + sum(x.bytes for x in rtypes),
            types=types,
            recoverable=ContainerRecoverableOut(
                count=sum(x.count for x in rtypes), bytes=sum(x.bytes for x in rtypes), types=rtypes),
            selected=chosen is not None,
            estimate=estimate,
        ))
    return outs


@router.get("/ingest/{job_id}/status", response_model=IngestJobOut)
async def get_ingest_status(
    job_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Poll ingest job progress."""
    job = await db.get(IngestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    from app.services.ingest import sources_attempted
    from app.services.large_ingest_estimate import estimate_model

    prod = await db.get(Production, job.production_id)
    await _maybe_schedule_self_heal(db, job, background_tasks)
    is_large = bool((job.field_mapping or {}).get("large_containers"))

    return IngestJobOut(
        id=job.id,
        production_id=job.production_id,
        production_name=prod.name if prod else "",
        status=job.status,
        total_files=job.total_files,
        processed_files=job.processed_files,
        skipped_files=job.skipped_files,
        files_done=sources_attempted(job),
        errors=job.errors or [],
        created_at=job.created_at,
        completed_at=job.completed_at,
        items_total=job.items_total or 0,
        items_done=job.items_done or 0,
        items_failed=job.items_failed or 0,
        containers=_container_outs(job),
        estimate_model=estimate_model() if is_large else {},
    )


@router.get("/productions/{production_id}/ingest-awaiting")
async def ingest_awaiting(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Large-container loads of the caller's waiting for their item-type choice."""
    rows = (await db.execute(
        select(IngestJob).where(
            IngestJob.production_id == production_id,
            IngestJob.user_id == user.id,
            IngestJob.status == "awaiting_selection",
        ).order_by(IngestJob.created_at)
    )).scalars().all()
    out = []
    for job in rows:
        for c in _container_outs(job):
            if not c.selected:
                out.append({"job_id": str(job.id), "name": c.name, "size_bytes": c.size_bytes})
    return out


@router.post("/ingest/reocr/{production_id}")
async def reocr_production(
    production_id: int,
    background_tasks: BackgroundTasks,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-run Cloud Vision OCR on documents in a production.

    Documents that are already fully OCR'd are skipped so their pages aren't
    billed to Cloud Vision again; pass ``force=true`` to re-OCR everything
    (e.g. after an OCR-pipeline change).

    Enqueues one Cloud Task per batch so the work survives container
    scale-down. Falls back to inline BackgroundTask if Cloud Tasks
    isn't configured.
    """
    from app.services import tasks as task_service

    production = await db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=404, detail="Production not found")
    if production.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    from sqlalchemy import func
    doc_count = await db.scalar(
        select(func.count()).select_from(Document).where(Document.production_id == production_id)
    )

    if task_service.is_configured():
        batch_size = 25
        enqueued = 0
        for offset in range(0, doc_count, batch_size):
            task_service.enqueue_reocr_batch(production_id, offset, batch_size, force=force)
            enqueued += 1
        return {"message": f"Re-OCR enqueued {enqueued} batches for {doc_count} documents", "production_id": production_id}

    background_tasks.add_task(run_reocr, production_id=production_id, force=force)

    return {"message": f"Re-OCR started for {doc_count} documents", "production_id": production_id}


@router.post("/ingest/reocr-batch")
async def reocr_batch_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker: re-OCR a batch of documents."""
    from app.services import ingest as ingest_service

    production_id = body.get("production_id")
    offset = body.get("offset", 0)
    limit = body.get("limit", 25)
    force = bool(body.get("force", False))

    result = await db.execute(
        select(Document)
        .where(Document.production_id == production_id)
        .order_by(Document.id)
        .offset(offset)
        .limit(limit)
    )
    docs = list(result.scalars().all())
    logger.info("Re-OCR batch: production %d, offset %d, %d docs", production_id, offset, len(docs))

    reocred = 0
    skipped = 0
    for doc in docs:
        try:
            if not doc.image_paths:
                continue
            if not force and ingest_service.document_ocr_complete(doc):
                skipped += 1
                continue
            page_errors: list[str] = []
            text_parts, ocr_paths = ingest_service.ocr_pages_with_sidecars(
                doc.production_id, doc.image_paths, doc.bates_begin, page_errors
            )
            for err in page_errors:
                logger.warning("Re-OCR: %s", err)
            doc.ocr_paths = ocr_paths
            if text_parts:
                doc.text_content = "\n\n".join(text_parts)
                await db.execute(
                    text(
                        "UPDATE documents SET text_search_vector = "
                        f"{ingest_service.tsvector_sql(':txt')} "
                        "WHERE id = :id"
                    ),
                    {"txt": doc.text_content, "id": doc.id},
                )
            reocred += 1
            await db.commit()
        except Exception:
            logger.exception("Re-OCR failed for doc %s", doc.bates_begin)
            await db.rollback()

    logger.info(
        "Re-OCR batch done: production %d, offset %d — %d re-OCR'd, %d already processed",
        production_id, offset, reocred, skipped,
    )
    return {"ok": True, "processed": reocred, "skipped": skipped}


async def run_reocr(production_id: int, force: bool = False):
    """Background task fallback: re-OCR all documents in a production using Cloud Vision."""
    from app.database import async_session_factory
    from app.services import ingest as ingest_service

    async with async_session_factory() as db:
        result = await db.execute(
            select(Document).where(Document.production_id == production_id)
        )
        docs = list(result.scalars().all())
        logger.info("Re-OCR: processing %d documents for production %d", len(docs), production_id)

        skipped = 0
        for i, doc in enumerate(docs):
            try:
                if not doc.image_paths:
                    continue
                if not force and ingest_service.document_ocr_complete(doc):
                    skipped += 1
                    continue
                page_errors: list[str] = []
                text_parts, ocr_paths = ingest_service.ocr_pages_with_sidecars(
                    production_id, doc.image_paths, doc.bates_begin, page_errors
                )
                for err in page_errors:
                    logger.warning("Re-OCR: %s", err)
                doc.ocr_paths = ocr_paths
                if text_parts:
                    doc.text_content = "\n\n".join(text_parts)
                    await db.execute(
                        text(
                            "UPDATE documents SET text_search_vector = "
                            f"{ingest_service.tsvector_sql(':txt')} "
                            "WHERE id = :id"
                        ),
                        {"txt": doc.text_content, "id": doc.id},
                    )
                await db.commit()
                if (i + 1) % 25 == 0:
                    logger.info("Re-OCR: %d/%d done", i + 1, len(docs))
            except Exception:
                logger.exception("Re-OCR failed for doc %s", doc.bates_begin)
                await db.rollback()

        logger.info(
            "Re-OCR complete for production %d — %d already processed and skipped",
            production_id, skipped,
        )


async def run_ingest_job(job_id: str, production_id: int, production_name: str):
    """Background task for the fallback ingest path."""
    from app.database import async_session_factory
    from app.services.ingest import ingest_from_storage

    async with async_session_factory() as db:
        try:
            await ingest_from_storage(db, job_id, production_id, production_name)
        except Exception as e:
            logger.exception("Ingest job failed")
            job = await db.get(IngestJob, job_id)
            if job:
                job.status = "failed"
                job.errors = (job.errors or []) + [str(e)]
                await db.commit()
