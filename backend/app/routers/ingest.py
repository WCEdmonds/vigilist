"""Ingest endpoints: start processing, process batch, check status."""

import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
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
    try:
        return await run_in_threadpool(analyze_load_file, int(production_id))
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
    batch_size = 10 if source_format in ("generic_pdf", "native") else INGEST_BATCH_SIZE

    if task_service.is_configured():
        # Count source items to set an accurate total_files, then enqueue tasks
        try:
            if source_format == "generic_pdf":
                from app.services.ingest_pdf import list_pdf_sources
                total_files = len(list_pdf_sources(production.id))
            elif source_format == "native":
                from app.services.ingest_native import list_native_sources
                total_files = len(list_native_sources(production.id))
            else:
                records, _ = bootstrap_ingest_source(production.id)
                total_files = len(records)
        except Exception as e:
            logger.exception("Failed to parse ingest source")
            raise HTTPException(status_code=400, detail=f"Failed to parse production files: {e}")

        if total_files == 0:
            raise HTTPException(status_code=400, detail="No ingestable files found in upload")

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
    from app.services.pipeline import run_ambient_pipeline

    await run_ambient_pipeline(int(production_id), force)
    return {"ok": True}


@router.post("/productions/{production_id}/extract-entities")
async def trigger_entity_extraction(
    production_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Backfill/manual trigger: run the ambient pipeline (which now includes
    the entities stage) for this production. Manager or admin only."""
    from app.dependencies import ROLE_RANK, get_user_role_for_production
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    from app.services import tasks as task_service
    if task_service.is_configured():
        task_service.enqueue_pipeline(production_id)
        await log_action(db, user, "entity_extraction_triggered", "production", str(production_id),
                         production_id=production_id, details={"mode": "enqueued"})
        await db.commit()
        return {"status": "enqueued"}

    from app.services.pipeline import run_ambient_pipeline
    background_tasks.add_task(run_ambient_pipeline, production_id)
    await log_action(db, user, "entity_extraction_triggered", "production", str(production_id),
                     production_id=production_id, details={"mode": "background"})
    await db.commit()
    return {"status": "started"}


@router.get("/ingest/{job_id}/status", response_model=IngestJobOut)
async def get_ingest_status(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Poll ingest job progress."""
    job = await db.get(IngestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    prod = await db.get(Production, job.production_id)

    return IngestJobOut(
        id=job.id,
        production_id=job.production_id,
        production_name=prod.name if prod else "",
        status=job.status,
        total_files=job.total_files,
        processed_files=job.processed_files,
        skipped_files=job.skipped_files,
        errors=job.errors or [],
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


@router.post("/ingest/reocr/{production_id}")
async def reocr_production(
    production_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-run Cloud Vision OCR on all documents in a production.

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
            task_service.enqueue_reocr_batch(production_id, offset, batch_size)
            enqueued += 1
        return {"message": f"Re-OCR enqueued {enqueued} batches for {doc_count} documents", "production_id": production_id}

    background_tasks.add_task(run_reocr, production_id=production_id)

    return {"message": f"Re-OCR started for {doc_count} documents", "production_id": production_id}


@router.post("/ingest/reocr-batch")
async def reocr_batch_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker: re-OCR a batch of documents."""
    from app.services.ocr import ocr_image_vision_bytes
    from app.services.storage import get_download_bytes

    production_id = body.get("production_id")
    offset = body.get("offset", 0)
    limit = body.get("limit", 25)

    result = await db.execute(
        select(Document)
        .where(Document.production_id == production_id)
        .order_by(Document.id)
        .offset(offset)
        .limit(limit)
    )
    docs = list(result.scalars().all())
    logger.info("Re-OCR batch: production %d, offset %d, %d docs", production_id, offset, len(docs))

    for doc in docs:
        try:
            if not doc.image_paths:
                continue
            text_parts = []
            for img_path in doc.image_paths:
                if not img_path:
                    continue
                img_bytes = get_download_bytes(img_path)
                page_text = ocr_image_vision_bytes(img_bytes)
                if page_text:
                    text_parts.append(page_text)
            if text_parts:
                doc.text_content = "\n\n".join(text_parts)
                await db.execute(
                    text(
                        "UPDATE documents SET text_search_vector = "
                        "to_tsvector('english', COALESCE(:txt, '')) "
                        "WHERE id = :id"
                    ),
                    {"txt": doc.text_content, "id": doc.id},
                )
                await db.commit()
        except Exception:
            logger.exception("Re-OCR failed for doc %s", doc.bates_begin)
            await db.rollback()

    return {"ok": True, "processed": len(docs)}


async def run_reocr(production_id: int):
    """Background task fallback: re-OCR all documents in a production using Cloud Vision."""
    from app.database import async_session_factory
    from app.services.ocr import ocr_image_vision_bytes
    from app.services.storage import get_download_bytes

    async with async_session_factory() as db:
        result = await db.execute(
            select(Document).where(Document.production_id == production_id)
        )
        docs = list(result.scalars().all())
        logger.info("Re-OCR: processing %d documents for production %d", len(docs), production_id)

        for i, doc in enumerate(docs):
            try:
                if not doc.image_paths:
                    continue
                text_parts = []
                for img_path in doc.image_paths:
                    if not img_path:
                        continue
                    img_bytes = get_download_bytes(img_path)
                    page_text = ocr_image_vision_bytes(img_bytes)
                    if page_text:
                        text_parts.append(page_text)
                if text_parts:
                    doc.text_content = "\n\n".join(text_parts)
                    await db.execute(
                        text(
                            "UPDATE documents SET text_search_vector = "
                            "to_tsvector('english', COALESCE(:txt, '')) "
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

        logger.info("Re-OCR complete for production %d", production_id)


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
