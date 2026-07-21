from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_user_role_for_production, ROLE_RANK, get_accessible_production_ids
from app.models import (
    Document, DocumentCluster, DocumentClusterAssignment,
    DocumentDuplicate, DocumentTag, DuplicateGroup, User,
)
from app.routers.auth import get_current_user
from app.schemas import (
    ClusterDocumentOut, ClusterOut, DuplicateEntryOut, FamilyMemberOut,
    FamilyThreadOut, PropagateTagRequest,
)
from app.services.audit import log_action

router = APIRouter(prefix="/api", tags=["intelligence"])


@router.post("/productions/{production_id}/detect-duplicates")
async def detect_duplicates_endpoint(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    from app.services.duplicates import detect_duplicates
    result = await detect_duplicates(db, production_id)

    await log_action(db, user, "duplicates_detected", "production", str(production_id),
                     production_id=production_id, details=result)
    await db.commit()
    return {"status": "complete", **result}


@router.post("/productions/{production_id}/cluster")
async def cluster_endpoint(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    from app.services.clustering import cluster_production
    clusters = await cluster_production(db, production_id)

    await log_action(db, user, "production_clustered", "production", str(production_id),
                     production_id=production_id, details={"cluster_count": len(clusters)})
    await db.commit()
    return {"status": "complete", "clusters": clusters}


@router.get("/productions/{production_id}/clusters", response_model=list[ClusterOut])
async def list_clusters(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await get_user_role_for_production(db, user, production_id)
    result = await db.execute(
        select(
            DocumentCluster,
            func.coalesce(func.sum(Document.page_count), 0).label("page_count"),
        )
        .outerjoin(DocumentClusterAssignment, DocumentCluster.id == DocumentClusterAssignment.cluster_id)
        .outerjoin(Document, DocumentClusterAssignment.document_id == Document.id)
        .where(DocumentCluster.production_id == production_id)
        .group_by(DocumentCluster.id)
        .order_by(DocumentCluster.doc_count.desc())
    )
    rows = result.all()
    return [
        ClusterOut(
            id=cluster.id,
            cluster_index=cluster.cluster_index,
            label=cluster.label,
            doc_count=cluster.doc_count,
            page_count=page_count,
        )
        for cluster, page_count in rows
    ]


def clamp_limit(limit: int) -> int:
    return max(1, min(20, limit))


@router.get(
    "/productions/{production_id}/clusters/{cluster_id}/documents",
    response_model=list[ClusterDocumentOut],
)
async def list_cluster_documents(
    production_id: int,
    cluster_id: int,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await get_user_role_for_production(db, user, production_id)
    cluster = await db.get(DocumentCluster, cluster_id)
    if cluster is None or cluster.production_id != production_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    rows = (
        await db.execute(
            select(Document.id, Document.bates_begin, Document.title)
            .join(
                DocumentClusterAssignment,
                DocumentClusterAssignment.document_id == Document.id,
            )
            .where(DocumentClusterAssignment.cluster_id == cluster_id)
            .order_by(Document.bates_begin)
            .limit(clamp_limit(limit))
        )
    ).all()
    return [
        ClusterDocumentOut(document_id=str(r[0]), bates_begin=r[1], title=r[2])
        for r in rows
    ]


@router.get("/documents/{doc_id}/duplicates", response_model=list[DuplicateEntryOut])
async def get_document_duplicates(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc or doc.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Document not found")

    # Find all groups this doc belongs to
    group_result = await db.execute(
        select(DocumentDuplicate.group_id, DocumentDuplicate.similarity, DuplicateGroup.type)
        .join(DuplicateGroup, DocumentDuplicate.group_id == DuplicateGroup.id)
        .where(DocumentDuplicate.document_id == doc_id)
    )
    groups = group_result.all()

    if not groups:
        return []

    # Get all other members of those groups
    group_ids = [g[0] for g in groups]
    members_result = await db.execute(
        select(DocumentDuplicate, Document.bates_begin, Document.title, Document.custodian, DuplicateGroup.type)
        .join(Document, DocumentDuplicate.document_id == Document.id)
        .join(DuplicateGroup, DocumentDuplicate.group_id == DuplicateGroup.id)
        .where(DocumentDuplicate.group_id.in_(group_ids))
        .where(DocumentDuplicate.document_id != doc_id)
    )

    return [
        DuplicateEntryOut(
            document_id=dd.document_id, bates_begin=bates,
            title=title, similarity=dd.similarity, type=dup_type,
            custodian=custodian,
        )
        for dd, bates, title, custodian, dup_type in members_result.all()
    ]


@router.get("/documents/{doc_id}/family", response_model=FamilyThreadOut)
async def get_document_family(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc or doc.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Document not found")

    async def _members(id_col, id_val):
        if not id_val:
            return []
        result = await db.execute(
            select(Document.id, Document.bates_begin, Document.title, Document.is_inclusive)
            .where(id_col == id_val)
            .where(Document.production_id.in_(accessible))
            .where(Document.id != doc_id)
            .order_by(Document.bates_begin)
        )
        return [
            FamilyMemberOut(document_id=r[0], bates_begin=r[1], title=r[2], is_inclusive=r[3])
            for r in result.all()
        ]

    return FamilyThreadOut(
        family=await _members(Document.family_id, doc.family_id),
        thread=await _members(Document.thread_id, doc.thread_id),
    )


@router.post("/documents/{doc_id}/propagate-tag")
async def propagate_tag(
    doc_id: UUID,
    body: PropagateTagRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc or doc.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Document not found")

    role = await get_user_role_for_production(db, user, doc.production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")

    # Find related document IDs
    related_ids = []
    if body.relationship_type == "duplicate":
        group_result = await db.execute(
            select(DocumentDuplicate.group_id)
            .where(DocumentDuplicate.document_id == doc_id)
        )
        group_ids = [r[0] for r in group_result.all()]
        if group_ids:
            members = await db.execute(
                select(DocumentDuplicate.document_id)
                .where(DocumentDuplicate.group_id.in_(group_ids))
                .where(DocumentDuplicate.document_id != doc_id)
            )
            related_ids = [r[0] for r in members.all()]
    elif body.relationship_type == "family" and doc.family_id:
        members = await db.execute(
            select(Document.id)
            .where(Document.family_id == doc.family_id)
            .where(Document.production_id == doc.production_id)
            .where(Document.id != doc_id)
        )
        related_ids = [r[0] for r in members.all()]
    elif body.relationship_type == "thread" and doc.thread_id:
        members = await db.execute(
            select(Document.id)
            .where(Document.thread_id == doc.thread_id)
            .where(Document.production_id == doc.production_id)
            .where(Document.id != doc_id)
        )
        related_ids = [r[0] for r in members.all()]

    # Apply tag to each related document
    tagged = 0
    for rel_id in related_ids:
        existing = await db.execute(
            select(DocumentTag).where(
                DocumentTag.document_id == rel_id,
                DocumentTag.tag_id == body.tag_id,
            )
        )
        if existing.scalar_one_or_none():
            continue
        db.add(DocumentTag(document_id=rel_id, tag_id=body.tag_id, applied_by=user.id))
        await log_action(db, user, "tag_applied", "document_tag", None,
                         production_id=doc.production_id,
                         details={
                             "document_id": str(rel_id), "tag_id": body.tag_id,
                             "propagated": True, "source_document_id": str(doc_id),
                             "relationship_type": body.relationship_type,
                         })
        tagged += 1

    await db.commit()
    return {"tagged_count": tagged}
