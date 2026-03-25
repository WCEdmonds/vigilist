"""Batch assignment, document listing, and review status endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_user_role_for_production
from app.models import BatchDocument, Document, ReviewBatch, ReviewQueue, User
from app.routers.auth import get_current_user
from app.schemas import (
    BatchAssignRequest,
    BatchDocumentOut,
    BatchDocumentUpdate,
    ReviewBatchOut,
)
from app.services.audit import log_action

router = APIRouter(prefix="/api/batches", tags=["batches"])


async def _build_batch_out(db: AsyncSession, batch: ReviewBatch) -> ReviewBatchOut:
    """Build a ReviewBatchOut, joining queue name and reviewer email."""
    queue = await db.get(ReviewQueue, batch.queue_id)
    queue_name = queue.name if queue else ""

    reviewer_email: str | None = None
    if batch.reviewer_id:
        reviewer = await db.get(User, batch.reviewer_id)
        reviewer_email = reviewer.email if reviewer else None

    return ReviewBatchOut(
        id=batch.id,
        queue_id=batch.queue_id,
        queue_name=queue_name,
        reviewer_id=batch.reviewer_id,
        reviewer_email=reviewer_email,
        status=batch.status,
        size=batch.size,
        reviewed_count=batch.reviewed_count,
        assigned_at=batch.assigned_at,
        completed_at=batch.completed_at,
        created_at=batch.created_at,
    )


@router.get("/my", response_model=list[ReviewBatchOut])
async def list_my_batches(
    production_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List batches assigned to the current user that are pending or in_progress."""
    stmt = (
        select(ReviewBatch)
        .where(
            ReviewBatch.reviewer_id == user.id,
            ReviewBatch.status.in_(["pending", "in_progress"]),
        )
        .order_by(ReviewBatch.assigned_at.asc())
    )

    if production_id is not None:
        stmt = stmt.join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id).where(
            ReviewQueue.production_id == production_id
        )

    result = await db.execute(stmt)
    batches = result.scalars().all()
    return [await _build_batch_out(db, b) for b in batches]


@router.get("/{batch_id}", response_model=ReviewBatchOut)
async def get_batch(
    batch_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get batch details. Requires access to the batch's production."""
    batch = await db.get(ReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")

    # Verify user has access to this production
    await get_user_role_for_production(db, user, queue.production_id)

    return await _build_batch_out(db, batch)


@router.post("/{batch_id}/assign", response_model=ReviewBatchOut)
async def assign_batch(
    batch_id: int,
    body: BatchAssignRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Assign a batch to a reviewer. Requires manager+ role."""
    batch = await db.get(ReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")

    role = await get_user_role_for_production(db, user, queue.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    batch.reviewer_id = body.reviewer_id
    batch.status = "in_progress"
    batch.assigned_at = func.now()

    await log_action(
        db,
        user,
        "batch_assigned",
        "review_batch",
        resource_id=str(batch_id),
        production_id=queue.production_id,
        details={"reviewer_id": body.reviewer_id},
    )
    await db.commit()
    await db.refresh(batch)
    return await _build_batch_out(db, batch)


@router.get("/{batch_id}/documents", response_model=list[BatchDocumentOut])
async def list_batch_documents(
    batch_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List documents in a batch, ordered by position.
    Only the assigned reviewer or manager+ can see.
    """
    batch = await db.get(ReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")

    role = await get_user_role_for_production(db, user, queue.production_id)

    is_assigned_reviewer = batch.reviewer_id == user.id
    is_manager_plus = ROLE_RANK.get(role, 0) >= ROLE_RANK["manager"]
    if not is_assigned_reviewer and not is_manager_plus:
        raise HTTPException(status_code=403, detail="Access denied to this batch")

    result = await db.execute(
        select(BatchDocument, Document.bates_begin, Document.title)
        .join(Document, BatchDocument.document_id == Document.id)
        .where(BatchDocument.batch_id == batch_id)
        .order_by(BatchDocument.position.asc())
    )
    rows = result.all()

    return [
        BatchDocumentOut(
            id=bd.id,
            batch_id=bd.batch_id,
            document_id=bd.document_id,
            position=bd.position,
            reviewed=bd.reviewed,
            reviewed_at=bd.reviewed_at,
            bates_begin=bates_begin,
            title=title,
        )
        for bd, bates_begin, title in rows
    ]


@router.put("/{batch_id}/documents/{doc_id}")
async def update_batch_document(
    batch_id: int,
    doc_id: int,
    body: BatchDocumentUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mark a document as reviewed or skipped.
    Only the assigned reviewer or manager+ can update.
    Returns dict with next_batch_id if the batch completed and a new batch was auto-assigned.
    """
    batch = await db.get(ReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")

    role = await get_user_role_for_production(db, user, queue.production_id)

    is_assigned_reviewer = batch.reviewer_id == user.id
    is_manager_plus = ROLE_RANK.get(role, 0) >= ROLE_RANK["manager"]
    if not is_assigned_reviewer and not is_manager_plus:
        raise HTTPException(status_code=403, detail="Access denied to this batch")

    bd = await db.get(BatchDocument, doc_id)
    if not bd or bd.batch_id != batch_id:
        raise HTTPException(status_code=404, detail="BatchDocument not found")

    was_pending = bd.reviewed == "pending"
    bd.reviewed = body.reviewed
    bd.reviewed_at = datetime.utcnow()

    # Increment reviewed_count if transitioning from pending
    if was_pending and body.reviewed in ("reviewed", "skipped"):
        batch.reviewed_count = (batch.reviewed_count or 0) + 1

    next_batch_id: int | None = None

    # Check if batch is now complete
    if batch.reviewed_count >= batch.size:
        batch.status = "completed"
        batch.completed_at = datetime.utcnow()

        # Auto-assign next pending batch in the same queue to the same reviewer
        if batch.reviewer_id:
            next_result = await db.execute(
                select(ReviewBatch)
                .where(
                    ReviewBatch.queue_id == batch.queue_id,
                    ReviewBatch.status == "pending",
                    ReviewBatch.reviewer_id.is_(None),
                )
                .order_by(ReviewBatch.id.asc())
                .limit(1)
            )
            next_batch = next_result.scalar_one_or_none()
            if next_batch:
                next_batch.reviewer_id = batch.reviewer_id
                next_batch.status = "in_progress"
                next_batch.assigned_at = func.now()
                next_batch_id = next_batch.id

    await log_action(
        db,
        user,
        "document_reviewed",
        "batch_document",
        resource_id=str(doc_id),
        production_id=queue.production_id,
        details={"batch_id": batch_id, "reviewed": body.reviewed},
    )
    await db.commit()

    return {
        "id": bd.id,
        "batch_id": bd.batch_id,
        "document_id": str(bd.document_id),
        "position": bd.position,
        "reviewed": bd.reviewed,
        "reviewed_at": bd.reviewed_at.isoformat() if bd.reviewed_at else None,
        "next_batch_id": next_batch_id,
    }
