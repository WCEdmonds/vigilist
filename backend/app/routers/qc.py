"""QC sampling, context, and decision endpoints."""

import random

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_user_role_for_production
from app.models import (
    BatchDocument,
    Document,
    DocumentTag,
    QCDecision,
    ReviewBatch,
    ReviewQueue,
    Tag,
    User,
)
from app.routers.auth import get_current_user
from app.schemas import QCDecisionCreate, QCDecisionOut, QCSampleRequest
from app.services.audit import log_action

router = APIRouter(prefix="/api/qc", tags=["qc"])


@router.post("/sample", response_model=list[int])
async def create_qc_sample(
    body: QCSampleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a QC sample. Returns list of batch_document IDs. Requires manager+ role."""
    queue = await db.get(ReviewQueue, body.queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")

    role = await get_user_role_for_production(db, user, queue.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    # Find reviewed BatchDocuments in the queue
    stmt = (
        select(BatchDocument.id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .where(
            ReviewBatch.queue_id == body.queue_id,
            BatchDocument.reviewed.in_(["reviewed", "skipped"]),
        )
    )

    # Optionally filter by reviewer
    if body.reviewer_id is not None:
        stmt = stmt.where(ReviewBatch.reviewer_id == body.reviewer_id)

    result = await db.execute(stmt)
    reviewed_ids = [row[0] for row in result.all()]

    # Exclude already QC'd batch documents
    if reviewed_ids:
        qc_result = await db.execute(
            select(QCDecision.batch_document_id).where(
                QCDecision.batch_document_id.in_(reviewed_ids)
            )
        )
        already_qcd = {row[0] for row in qc_result.all()}
        eligible_ids = [bid for bid in reviewed_ids if bid not in already_qcd]
    else:
        eligible_ids = []

    if not eligible_ids:
        return []

    # Random sample based on sample_percent
    sample_count = max(1, round(len(eligible_ids) * body.sample_percent / 100.0))
    sample_count = min(sample_count, len(eligible_ids))
    sampled = random.sample(eligible_ids, sample_count)

    await log_action(
        db,
        user,
        "qc_sample_created",
        "review_queue",
        resource_id=str(body.queue_id),
        production_id=queue.production_id,
        details={
            "queue_id": body.queue_id,
            "sample_percent": body.sample_percent,
            "eligible_count": len(eligible_ids),
            "sampled_count": len(sampled),
            "reviewer_id": body.reviewer_id,
        },
    )
    await db.commit()

    return sampled


@router.get("/batch-document/{bd_id}")
async def get_qc_context(
    bd_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """QC context for a batch document. Requires access to the production."""
    bd = await db.get(BatchDocument, bd_id)
    if not bd:
        raise HTTPException(status_code=404, detail="BatchDocument not found")

    batch = await db.get(ReviewBatch, bd.batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="ReviewBatch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")

    await get_user_role_for_production(db, user, queue.production_id)

    doc = await db.get(Document, bd.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Original reviewer info
    original_reviewer_id = batch.reviewer_id
    original_reviewer_email: str | None = None
    if original_reviewer_id:
        reviewer = await db.get(User, original_reviewer_id)
        original_reviewer_email = reviewer.email if reviewer else None

    # Current tags
    tags_result = await db.execute(
        select(Tag.id, Tag.name, Tag.category)
        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .where(DocumentTag.document_id == bd.document_id)
    )
    current_tags = [
        {"id": row[0], "name": row[1], "category": row[2]}
        for row in tags_result.all()
    ]

    # Existing QC decision (if any)
    existing_result = await db.execute(
        select(QCDecision).where(QCDecision.batch_document_id == bd_id).limit(1)
    )
    existing_decision_row = existing_result.scalar_one_or_none()
    existing_decision = None
    if existing_decision_row:
        existing_decision = {
            "id": existing_decision_row.id,
            "decision": existing_decision_row.decision,
            "reason": existing_decision_row.reason,
            "created_at": existing_decision_row.created_at,
        }

    return {
        "batch_document_id": bd_id,
        "document_id": str(bd.document_id),
        "bates_begin": doc.bates_begin,
        "title": doc.title,
        "original_reviewer_id": original_reviewer_id,
        "original_reviewer_email": original_reviewer_email,
        "current_tags": current_tags,
        "existing_decision": existing_decision,
    }


@router.post("/batch-document/{bd_id}/decide", response_model=QCDecisionOut)
async def record_qc_decision(
    bd_id: int,
    body: QCDecisionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record a QC decision. Requires manager+ role."""
    bd = await db.get(BatchDocument, bd_id)
    if not bd:
        raise HTTPException(status_code=404, detail="BatchDocument not found")

    batch = await db.get(ReviewBatch, bd.batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="ReviewBatch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")

    role = await get_user_role_for_production(db, user, queue.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    # Validate decision value
    if body.decision not in ("agree", "overturn"):
        raise HTTPException(
            status_code=422, detail="Decision must be 'agree' or 'overturn'"
        )

    # If overturn, reason is required
    if body.decision == "overturn" and not body.reason:
        raise HTTPException(
            status_code=422, detail="Reason is required when overturning a decision"
        )

    # Snapshot current tags
    tags_result = await db.execute(
        select(Tag.id, Tag.name, Tag.category)
        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .where(DocumentTag.document_id == bd.document_id)
    )
    original_tags_snapshot = [
        {"id": row[0], "name": row[1], "category": row[2]}
        for row in tags_result.all()
    ]

    # Apply new tags if overturning
    new_tags_snapshot = None
    if body.decision == "overturn" and body.new_tag_ids is not None:
        # Delete existing tags for the document
        await db.execute(
            delete(DocumentTag).where(DocumentTag.document_id == bd.document_id)
        )

        # Fetch and apply new tags
        new_tags_snapshot = []
        for tag_id in body.new_tag_ids:
            tag = await db.get(Tag, tag_id)
            if tag:
                db.add(
                    DocumentTag(
                        document_id=bd.document_id,
                        tag_id=tag_id,
                        applied_by=user.id,
                    )
                )
                new_tags_snapshot.append(
                    {"id": tag.id, "name": tag.name, "category": tag.category}
                )

    # Original reviewer
    original_reviewer_id = batch.reviewer_id or user.id

    decision = QCDecision(
        batch_document_id=bd_id,
        original_reviewer_id=original_reviewer_id,
        qc_reviewer_id=user.id,
        decision=body.decision,
        reason=body.reason,
        original_tags=original_tags_snapshot,
        new_tags=new_tags_snapshot,
    )
    db.add(decision)
    await db.flush()

    # Resolve emails for response
    original_reviewer_email = ""
    if original_reviewer_id:
        orig_reviewer = await db.get(User, original_reviewer_id)
        original_reviewer_email = orig_reviewer.email if orig_reviewer else ""

    doc = await db.get(Document, bd.document_id)
    bates_begin = doc.bates_begin if doc else ""

    await log_action(
        db,
        user,
        "qc_decision",
        "batch_document",
        resource_id=str(bd_id),
        production_id=queue.production_id,
        details={
            "decision": body.decision,
            "original_reviewer_id": original_reviewer_id,
            "tags_changed": body.new_tag_ids is not None,
        },
    )
    await db.commit()
    await db.refresh(decision)

    return QCDecisionOut(
        id=decision.id,
        batch_document_id=decision.batch_document_id,
        original_reviewer_id=decision.original_reviewer_id,
        original_reviewer_email=original_reviewer_email,
        qc_reviewer_id=decision.qc_reviewer_id,
        qc_reviewer_email=user.email,
        decision=decision.decision,
        reason=decision.reason,
        original_tags=decision.original_tags,
        new_tags=decision.new_tags,
        created_at=decision.created_at,
        bates_begin=bates_begin,
    )
