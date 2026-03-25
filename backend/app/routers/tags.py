from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Document, DocumentTag, Tag
from app.routers.auth import get_current_user
from app.schemas import (
    ApplyTagsRequest,
    BulkTagRequest,
    DocumentTagOut,
    TagCreate,
    TagOut,
)

router = APIRouter(prefix="/api", tags=["tags"])


@router.get("/tags", response_model=list[TagOut])
async def list_tags(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(get_current_user),
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
    _user: str = Depends(get_current_user),
):
    tag = Tag(**body.model_dump())
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


@router.get("/documents/{doc_id}/tags", response_model=list[DocumentTagOut])
async def get_document_tags(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(get_current_user),
):
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
    user: str = Depends(get_current_user),
):
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    for tag_id in body.tag_ids:
        existing = await db.execute(
            select(DocumentTag).where(
                DocumentTag.document_id == doc_id,
                DocumentTag.tag_id == tag_id,
            )
        )
        if existing.scalar_one_or_none():
            continue
        dt = DocumentTag(document_id=doc_id, tag_id=tag_id, applied_by=user)
        db.add(dt)

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
    _user: str = Depends(get_current_user),
):
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
    await db.commit()
    return {"ok": True}


@router.post("/documents/bulk-tag")
async def bulk_tag(
    body: BulkTagRequest,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
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
            db.add(DocumentTag(document_id=doc_id, tag_id=tag_id, applied_by=user))
            count += 1

    await db.commit()
    return {"tagged": count}
