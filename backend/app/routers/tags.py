from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Document, DocumentTag, Tag, User
from app.routers.auth import get_current_user
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.services.audit import log_action
from app.schemas import (
    ApplyTagsRequest,
    BulkTagRequest,
    DocumentTagOut,
    TagCreate,
    TagOut,
    TagPrivilegeUpdate,
)

router = APIRouter(prefix="/api", tags=["tags"])


@router.get("/tags", response_model=list[TagOut])
async def list_tags(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    query = select(Tag).order_by(Tag.category, Tag.name)
    if category:
        query = query.where(Tag.category == category)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/tags", response_model=TagOut)
async def create_tag(
    body: TagCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    tag = Tag(**body.model_dump())
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


@router.put("/tags/{tag_id}", response_model=TagOut)
async def update_tag_privilege(
    tag_id: int,
    body: TagPrivilegeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    if tag.production_id is not None:
        role = await get_user_role_for_production(db, user, tag.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Manager or higher role required")
    else:
        # Global tag: manager+ on at least one accessible production.
        accessible = await get_accessible_production_ids(db, user)
        for pid in accessible:
            role = await get_user_role_for_production(db, user, pid)
            if ROLE_RANK.get(role, 0) >= ROLE_RANK["manager"]:
                break
        else:
            raise HTTPException(status_code=403, detail="Manager or higher role required")
    tag.is_privilege = body.is_privilege
    await log_action(db, user, "tag_privilege_set", "tag", str(tag_id),
                     production_id=tag.production_id,
                     details={"is_privilege": body.is_privilege})
    await db.commit()
    await db.refresh(tag)
    return TagOut.model_validate(tag)


@router.get("/documents/{doc_id}/tags", response_model=list[DocumentTagOut])
async def get_document_tags(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    query = (
        select(DocumentTag)
        .where(DocumentTag.document_id == doc_id)
        .options(selectinload(DocumentTag.tag))
        .order_by(DocumentTag.applied_at)
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/documents/{doc_id}/tags", response_model=list[DocumentTagOut])
async def apply_tags(
    doc_id: UUID,
    body: ApplyTagsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, doc.production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")

    for tag_id in body.tag_ids:
        existing = await db.execute(
            select(DocumentTag).where(
                DocumentTag.document_id == doc_id,
                DocumentTag.tag_id == tag_id,
            )
        )
        if existing.scalar_one_or_none():
            continue
        dt = DocumentTag(document_id=doc_id, tag_id=tag_id, applied_by=user.id)
        db.add(dt)

    for tag_id in body.tag_ids:
        await log_action(db, user, "tag_applied", "document", str(doc_id),
                         production_id=doc.production_id, details={"tag_id": tag_id})
    await db.commit()

    # Return updated tag list
    query = (
        select(DocumentTag)
        .where(DocumentTag.document_id == doc_id)
        .options(selectinload(DocumentTag.tag))
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.delete("/documents/{doc_id}/tags/{tag_id}")
async def remove_tag(
    doc_id: UUID,
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, doc.production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")
    result = await db.execute(
        select(DocumentTag).where(
            DocumentTag.document_id == doc_id,
            DocumentTag.tag_id == tag_id,
        )
    )
    dt = result.scalar_one_or_none()
    if not dt:
        raise HTTPException(status_code=404, detail="Tag not applied")
    await db.delete(dt)
    await log_action(db, user, "tag_removed", "document", str(doc_id),
                     production_id=doc.production_id, details={"tag_id": tag_id})
    await db.commit()
    return {"ok": True}


@router.post("/documents/bulk-tag")
async def bulk_tag(
    body: BulkTagRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    # Verify all documents are accessible
    docs = []
    for doc_id in body.doc_ids:
        doc = await db.get(Document, doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
        if doc.production_id not in accessible:
            raise HTTPException(status_code=403, detail="Access denied")
        docs.append(doc)
    if docs:
        role = await get_user_role_for_production(db, user, docs[0].production_id)
        if role == "readonly":
            raise HTTPException(status_code=403, detail="Read-only access")
    count = 0
    for doc_id in body.doc_ids:
        for tag_id in body.tag_ids:
            existing = await db.execute(
                select(DocumentTag).where(
                    DocumentTag.document_id == doc_id,
                    DocumentTag.tag_id == tag_id,
                )
            )
            if existing.scalar_one_or_none():
                continue
            db.add(DocumentTag(document_id=doc_id, tag_id=tag_id, applied_by=user.id))
            count += 1

    await log_action(db, user, "bulk_tag_applied", "document", None,
                     details={"doc_ids": [str(d) for d in body.doc_ids], "tag_ids": body.tag_ids})
    await db.commit()
    return {"tagged": count}
