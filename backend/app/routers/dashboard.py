"""Dashboard endpoints: review progress, QC stats, and reviewer agreement."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, distinct, func, select
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
from app.schemas import DashboardStats, QCStats

router = APIRouter(
    prefix="/api/productions/{production_id}/dashboard",
    tags=["dashboard"],
)


@router.get("", response_model=DashboardStats)
async def get_dashboard_stats(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Overall review progress for a production."""
    await get_user_role_for_production(db, user, production_id)

    # Total documents in production
    total_result = await db.execute(
        select(func.count(Document.id)).where(Document.production_id == production_id)
    )
    total_documents: int = total_result.scalar_one() or 0

    # Reviewed documents: distinct batch_documents with reviewed="reviewed"
    # scoped to queues in this production
    reviewed_result = await db.execute(
        select(func.count(distinct(BatchDocument.document_id)))
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(
            ReviewQueue.production_id == production_id,
            BatchDocument.reviewed == "reviewed",
        )
    )
    reviewed_documents: int = reviewed_result.scalar_one() or 0

    pending_documents = total_documents - reviewed_documents
    percent_complete = (
        (reviewed_documents / total_documents * 100) if total_documents > 0 else 0.0
    )

    # Tag breakdown: group by category, name with counts
    tag_rows = await db.execute(
        select(Tag.category, Tag.name, func.count(DocumentTag.id).label("count"))
        .join(DocumentTag, Tag.id == DocumentTag.tag_id)
        .join(Document, DocumentTag.document_id == Document.id)
        .where(Document.production_id == production_id)
        .group_by(Tag.category, Tag.name)
        .order_by(Tag.category, Tag.name)
    )
    tag_breakdown: dict = {}
    for category, name, count in tag_rows.all():
        if category not in tag_breakdown:
            tag_breakdown[category] = {}
        tag_breakdown[category][name] = count

    # Reviewer stats: group by reviewer_id with sum of reviewed_count
    reviewer_rows = await db.execute(
        select(
            ReviewBatch.reviewer_id,
            func.sum(ReviewBatch.reviewed_count).label("reviewed_count"),
        )
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(
            ReviewQueue.production_id == production_id,
            ReviewBatch.reviewer_id.isnot(None),
        )
        .group_by(ReviewBatch.reviewer_id)
        .order_by(func.sum(ReviewBatch.reviewed_count).desc())
    )
    reviewer_stats = [
        {"reviewer_id": reviewer_id, "reviewed_count": int(reviewed_count or 0)}
        for reviewer_id, reviewed_count in reviewer_rows.all()
    ]

    # Queue stats: per queue — id, name, total, reviewed, batch_count
    queue_rows = await db.execute(
        select(
            ReviewQueue.id,
            ReviewQueue.name,
            func.sum(ReviewBatch.size).label("total"),
            func.sum(ReviewBatch.reviewed_count).label("reviewed"),
            func.count(ReviewBatch.id).label("batch_count"),
        )
        .outerjoin(ReviewBatch, ReviewQueue.id == ReviewBatch.queue_id)
        .where(ReviewQueue.production_id == production_id)
        .group_by(ReviewQueue.id, ReviewQueue.name)
        .order_by(ReviewQueue.id)
    )
    queue_stats = [
        {
            "id": queue_id,
            "name": name,
            "total": int(total or 0),
            "reviewed": int(reviewed or 0),
            "batch_count": int(batch_count or 0),
        }
        for queue_id, name, total, reviewed, batch_count in queue_rows.all()
    ]

    return DashboardStats(
        total_documents=total_documents,
        reviewed_documents=reviewed_documents,
        pending_documents=pending_documents,
        percent_complete=round(percent_complete, 2),
        tag_breakdown=tag_breakdown,
        reviewer_stats=reviewer_stats,
        queue_stats=queue_stats,
    )


@router.get("/qc", response_model=QCStats)
async def get_qc_stats(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """QC metrics for a production."""
    await get_user_role_for_production(db, user, production_id)

    # Aggregate QC decisions scoped to this production's queues
    agg_result = await db.execute(
        select(
            func.count(QCDecision.id).label("total_decisions"),
            func.sum(
                case((QCDecision.decision == "agree", 1), else_=0)
            ).label("agree_count"),
            func.sum(
                case((QCDecision.decision == "overturn", 1), else_=0)
            ).label("overturn_count"),
        )
        .join(BatchDocument, QCDecision.batch_document_id == BatchDocument.id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewQueue.production_id == production_id)
    )
    row = agg_result.one()
    total_decisions: int = int(row.total_decisions or 0)
    agree_count: int = int(row.agree_count or 0)
    overturn_count: int = int(row.overturn_count or 0)
    overturn_rate: float = (
        (overturn_count / total_decisions * 100) if total_decisions > 0 else 0.0
    )

    # By-reviewer breakdown
    by_reviewer_rows = await db.execute(
        select(
            QCDecision.original_reviewer_id,
            func.count(QCDecision.id).label("total"),
            func.sum(
                case((QCDecision.decision == "overturn", 1), else_=0)
            ).label("overturns"),
        )
        .join(BatchDocument, QCDecision.batch_document_id == BatchDocument.id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewQueue.production_id == production_id)
        .group_by(QCDecision.original_reviewer_id)
        .order_by(func.sum(case((QCDecision.decision == "overturn", 1), else_=0)).desc())
    )
    by_reviewer = []
    for original_reviewer_id, total, overturns in by_reviewer_rows.all():
        total_int = int(total or 0)
        overturns_int = int(overturns or 0)
        by_reviewer.append(
            {
                "reviewer_id": original_reviewer_id,
                "total": total_int,
                "overturns": overturns_int,
                "overturn_rate": round(
                    (overturns_int / total_int * 100) if total_int > 0 else 0.0, 2
                ),
            }
        )

    return QCStats(
        total_decisions=total_decisions,
        agree_count=agree_count,
        overturn_count=overturn_count,
        overturn_rate=round(overturn_rate, 2),
        by_reviewer=by_reviewer,
    )


@router.get("/agreement")
async def get_reviewer_agreement(
    production_id: int,
    reviewer_a: str,
    reviewer_b: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Reviewer agreement analysis between two reviewers. Requires manager+ role."""
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    # Find document_ids reviewed by reviewer_a in this production
    docs_a_result = await db.execute(
        select(distinct(BatchDocument.document_id))
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(
            ReviewQueue.production_id == production_id,
            ReviewBatch.reviewer_id == reviewer_a,
            BatchDocument.reviewed == "reviewed",
        )
    )
    docs_a = {row[0] for row in docs_a_result.all()}

    # Find document_ids reviewed by reviewer_b in this production
    docs_b_result = await db.execute(
        select(distinct(BatchDocument.document_id))
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(
            ReviewQueue.production_id == production_id,
            ReviewBatch.reviewer_id == reviewer_b,
            BatchDocument.reviewed == "reviewed",
        )
    )
    docs_b = {row[0] for row in docs_b_result.all()}

    overlapping = docs_a & docs_b
    overlap_count = len(overlapping)

    if overlap_count == 0:
        return {
            "overlap_count": 0,
            "agreement_rate": 0.0,
            "agree_count": 0,
            "disagree_count": 0,
            "details": [],
        }

    # For each overlapping document, collect tags applied by each reviewer
    # Tags applied by reviewer_a
    tags_a_result = await db.execute(
        select(DocumentTag.document_id, DocumentTag.tag_id)
        .where(
            DocumentTag.document_id.in_(overlapping),
            DocumentTag.applied_by == reviewer_a,
        )
    )
    tags_by_doc_a: dict = {}
    for doc_id, tag_id in tags_a_result.all():
        tags_by_doc_a.setdefault(doc_id, set()).add(tag_id)

    # Tags applied by reviewer_b
    tags_b_result = await db.execute(
        select(DocumentTag.document_id, DocumentTag.tag_id)
        .where(
            DocumentTag.document_id.in_(overlapping),
            DocumentTag.applied_by == reviewer_b,
        )
    )
    tags_by_doc_b: dict = {}
    for doc_id, tag_id in tags_b_result.all():
        tags_by_doc_b.setdefault(doc_id, set()).add(tag_id)

    agree_count = 0
    disagree_count = 0
    details = []

    for doc_id in overlapping:
        a_tags = tags_by_doc_a.get(doc_id, set())
        b_tags = tags_by_doc_b.get(doc_id, set())
        agreed = a_tags == b_tags

        if agreed:
            agree_count += 1
        else:
            disagree_count += 1

        if len(details) < 100:
            details.append(
                {
                    "document_id": str(doc_id),
                    "agreed": agreed,
                    "reviewer_a_tags": sorted(a_tags),
                    "reviewer_b_tags": sorted(b_tags),
                }
            )

    agreement_rate = round((agree_count / overlap_count * 100) if overlap_count > 0 else 0.0, 2)

    return {
        "overlap_count": overlap_count,
        "agreement_rate": agreement_rate,
        "agree_count": agree_count,
        "disagree_count": disagree_count,
        "details": details,
    }
