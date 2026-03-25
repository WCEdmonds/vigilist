"""Review queue CRUD and batch creation endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_user_role_for_production
from app.models import ReviewBatch, ReviewQueue, User
from app.routers.auth import get_current_user
from app.schemas import (
    BatchCreateRequest,
    ReviewBatchOut,
    ReviewQueueCreate,
    ReviewQueueOut,
)
from app.services.audit import log_action
from app.services.batching import create_batches

router = APIRouter(
    prefix="/api/productions/{production_id}/queues",
    tags=["queues"],
)


async def _build_queue_out(db: AsyncSession, q: ReviewQueue) -> ReviewQueueOut:
    """Build a ReviewQueueOut with computed batch/document stats."""
    row = await db.execute(
        select(
            func.count(ReviewBatch.id),
            func.coalesce(func.sum(ReviewBatch.size), 0),
            func.coalesce(func.sum(ReviewBatch.reviewed_count), 0),
        ).where(ReviewBatch.queue_id == q.id)
    )
    batch_count, total_documents, reviewed_documents = row.one()
    return ReviewQueueOut(
        id=q.id,
        production_id=q.production_id,
        name=q.name,
        description=q.description,
        query=q.query,
        filters=q.filters,
        status=q.status,
        created_by=q.created_by,
        created_at=q.created_at,
        batch_count=batch_count,
        total_documents=int(total_documents),
        reviewed_documents=int(reviewed_documents),
    )


@router.get("", response_model=list[ReviewQueueOut])
async def list_queues(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all review queues for a production. Requires any role."""
    await get_user_role_for_production(db, user, production_id)

    result = await db.execute(
        select(ReviewQueue)
        .where(ReviewQueue.production_id == production_id)
        .order_by(ReviewQueue.created_at.desc())
    )
    queues = result.scalars().all()
    return [await _build_queue_out(db, q) for q in queues]


@router.post("", response_model=ReviewQueueOut)
async def create_queue(
    production_id: int,
    body: ReviewQueueCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a review queue. Requires manager+ role."""
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    queue = ReviewQueue(
        production_id=production_id,
        name=body.name,
        description=body.description,
        query=body.query,
        filters=body.filters,
        created_by=user.id,
    )
    db.add(queue)
    await db.flush()

    await log_action(
        db,
        user,
        "queue_created",
        "review_queue",
        resource_id=str(queue.id),
        production_id=production_id,
        details={"name": queue.name},
    )
    await db.commit()
    await db.refresh(queue)
    return await _build_queue_out(db, queue)


@router.delete("/{queue_id}")
async def delete_queue(
    production_id: int,
    queue_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a review queue. Requires manager+ role."""
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    queue = await db.get(ReviewQueue, queue_id)
    if not queue or queue.production_id != production_id:
        raise HTTPException(status_code=404, detail="Queue not found")

    await db.delete(queue)
    await log_action(
        db,
        user,
        "queue_deleted",
        "review_queue",
        resource_id=str(queue_id),
        production_id=production_id,
        details={"name": queue.name},
    )
    await db.commit()
    return {"ok": True}


@router.get("/{queue_id}/batches", response_model=list[ReviewBatchOut])
async def list_queue_batches(
    production_id: int,
    queue_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all batches for a queue (manager/admin view)."""
    role = await get_user_role_for_production(db, user, production_id)

    queue = await db.get(ReviewQueue, queue_id)
    if not queue or queue.production_id != production_id:
        raise HTTPException(status_code=404, detail="Queue not found")

    result = await db.execute(
        select(ReviewBatch, User.email)
        .outerjoin(User, ReviewBatch.reviewer_id == User.id)
        .where(ReviewBatch.queue_id == queue_id)
        .order_by(ReviewBatch.created_at)
    )
    rows = result.all()

    return [
        ReviewBatchOut(
            id=batch.id,
            queue_id=batch.queue_id,
            queue_name=queue.name,
            reviewer_id=batch.reviewer_id,
            reviewer_email=email,
            status=batch.status,
            size=batch.size,
            reviewed_count=batch.reviewed_count,
            assigned_at=batch.assigned_at,
            completed_at=batch.completed_at,
            created_at=batch.created_at,
        )
        for batch, email in rows
    ]


@router.post("/{queue_id}/batches", response_model=list[ReviewBatchOut])
async def create_queue_batches(
    production_id: int,
    queue_id: int,
    body: BatchCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create batches from unbatched documents in a queue. Requires manager+ role."""
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    queue = await db.get(ReviewQueue, queue_id)
    if not queue or queue.production_id != production_id:
        raise HTTPException(status_code=404, detail="Queue not found")

    batches = await create_batches(
        db, queue, batch_size=body.batch_size, reviewer_id=body.reviewer_id
    )

    # Resolve reviewer email if a reviewer was assigned
    reviewer_email: str | None = None
    if body.reviewer_id:
        from app.models import User as UserModel
        reviewer = await db.get(UserModel, body.reviewer_id)
        reviewer_email = reviewer.email if reviewer else None

    await log_action(
        db,
        user,
        "batches_created",
        "review_queue",
        resource_id=str(queue_id),
        production_id=production_id,
        details={"batch_count": len(batches), "batch_size": body.batch_size},
    )
    await db.commit()

    return [
        ReviewBatchOut(
            id=b.id,
            queue_id=b.queue_id,
            queue_name=queue.name,
            reviewer_id=b.reviewer_id,
            reviewer_email=reviewer_email,
            status=b.status,
            size=b.size,
            reviewed_count=b.reviewed_count,
            assigned_at=b.assigned_at,
            completed_at=b.completed_at,
            created_at=b.created_at,
        )
        for b in batches
    ]
