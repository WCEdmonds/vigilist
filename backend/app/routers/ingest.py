"""Ingest endpoints: start processing, check status."""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import IngestJob, Production, User
from app.routers.auth import get_current_user
from app.schemas import IngestJobOut

router = APIRouter(prefix="/api", tags=["ingest"])


@router.post("/ingest", response_model=IngestJobOut)
async def start_ingest(
    body: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a production and start async ingest processing.

    Frontend has already uploaded files to Firebase Storage under
    productions/{production_id}/raw/. This endpoint creates the
    production record and kicks off background processing.
    """
    production_name = body.get("production_name", "").strip()
    description = body.get("description", "").strip()
    total_files = body.get("total_files", 0)

    if not production_name:
        raise HTTPException(status_code=400, detail="production_name is required")

    existing = await db.execute(
        select(Production).where(Production.name == production_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Production with this name already exists")

    production = Production(name=production_name, description=description, owner_id=user.id)
    db.add(production)
    await db.flush()

    job = IngestJob(
        production_id=production.id,
        user_id=user.id,
        status="processing",
        total_files=total_files,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await db.refresh(production)

    background_tasks.add_task(
        run_ingest_job,
        job_id=str(job.id),
        production_id=production.id,
        production_name=production_name,
    )

    return IngestJobOut(
        id=job.id,
        production_id=production.id,
        production_name=production_name,
        status=job.status,
        total_files=job.total_files,
        processed_files=0,
        errors=[],
        created_at=job.created_at,
        completed_at=None,
    )


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
        errors=job.errors or [],
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


async def run_ingest_job(job_id: str, production_id: int, production_name: str):
    """Background task that processes uploaded files from Firebase Storage."""
    from app.database import async_session_factory
    from app.services.ingest import ingest_from_storage
    import logging

    async with async_session_factory() as db:
        try:
            await ingest_from_storage(db, job_id, production_id, production_name)
        except Exception as e:
            logging.getLogger(__name__).exception("Ingest job failed")
            job = await db.get(IngestJob, job_id)
            if job:
                job.status = "failed"
                job.errors = (job.errors or []) + [str(e)]
                await db.commit()
