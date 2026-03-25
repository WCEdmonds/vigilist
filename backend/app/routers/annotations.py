from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Annotation, Document, User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids, get_user_role_for_production, ROLE_RANK
from app.services.audit import log_action
from app.schemas import AnnotationCreate, AnnotationOut, AnnotationUpdate

router = APIRouter(prefix="/api", tags=["annotations"])


def _build_annotation_out(ann: Annotation, email: str, display_name: str | None) -> AnnotationOut:
    return AnnotationOut(
        id=ann.id,
        document_id=ann.document_id,
        page_num=ann.page_num,
        x_pct=ann.x_pct,
        y_pct=ann.y_pct,
        color=ann.color,
        content=ann.content,
        created_by=ann.created_by,
        created_by_email=email,
        created_by_display_name=display_name,
        created_at=ann.created_at,
        updated_at=ann.updated_at,
    )


async def _resolve_user(db: AsyncSession, user_id: str) -> User | None:
    return await db.get(User, user_id)


@router.get("/documents/{doc_id}/annotations", response_model=list[AnnotationOut])
async def list_annotations(
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
        select(Annotation)
        .where(Annotation.document_id == doc_id)
        .order_by(Annotation.page_num.asc(), Annotation.created_at.asc())
    )
    result = await db.execute(query)
    annotations = result.scalars().all()

    # Resolve unique user emails/display_names in one pass
    user_cache: dict[str, User | None] = {}
    out: list[AnnotationOut] = []
    for ann in annotations:
        if ann.created_by not in user_cache:
            user_cache[ann.created_by] = await _resolve_user(db, ann.created_by)
        creator = user_cache[ann.created_by]
        email = creator.email if creator else ann.created_by
        display_name = creator.display_name if creator else None
        out.append(_build_annotation_out(ann, email, display_name))

    return out


@router.post("/documents/{doc_id}/annotations", response_model=AnnotationOut, status_code=201)
async def create_annotation(
    doc_id: UUID,
    body: AnnotationCreate,
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

    if body.page_num < 1 or body.page_num > doc.page_count:
        raise HTTPException(
            status_code=422,
            detail=f"page_num must be between 1 and {doc.page_count}",
        )
    if not (0.0 <= body.x_pct <= 100.0) or not (0.0 <= body.y_pct <= 100.0):
        raise HTTPException(status_code=422, detail="x_pct and y_pct must be between 0 and 100")

    ann = Annotation(
        document_id=doc_id,
        page_num=body.page_num,
        x_pct=body.x_pct,
        y_pct=body.y_pct,
        color=body.color,
        content=body.content,
        created_by=user.id,
    )
    db.add(ann)
    await db.flush()
    await log_action(
        db, user, "annotation_created", "annotation", str(ann.id),
        production_id=doc.production_id,
        details={"document_id": str(doc_id), "page_num": body.page_num},
    )
    await db.commit()
    await db.refresh(ann)

    return _build_annotation_out(ann, user.email, user.display_name)


@router.put("/annotations/{ann_id}", response_model=AnnotationOut)
async def update_annotation(
    ann_id: int,
    body: AnnotationUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    ann = await db.get(Annotation, ann_id)
    if not ann:
        raise HTTPException(status_code=404, detail="Annotation not found")

    doc = await db.get(Document, ann.document_id)
    if doc and doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    if ann.created_by != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can edit this annotation")

    if body.content is not None:
        ann.content = body.content
    if body.color is not None:
        ann.color = body.color

    await log_action(
        db, user, "annotation_updated", "annotation", str(ann_id),
        details={"document_id": str(ann.document_id)},
    )
    await db.commit()
    await db.refresh(ann)

    creator = await _resolve_user(db, ann.created_by)
    email = creator.email if creator else ann.created_by
    display_name = creator.display_name if creator else None
    return _build_annotation_out(ann, email, display_name)


@router.delete("/annotations/{ann_id}")
async def delete_annotation(
    ann_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    ann = await db.get(Annotation, ann_id)
    if not ann:
        raise HTTPException(status_code=404, detail="Annotation not found")

    doc = await db.get(Document, ann.document_id)
    if doc and doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    is_creator = ann.created_by == user.id
    if not is_creator:
        if not doc:
            raise HTTPException(status_code=403, detail="Access denied")
        role = await get_user_role_for_production(db, user, doc.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(
                status_code=403,
                detail="Only the creator or a manager can delete this annotation",
            )

    await db.delete(ann)
    await log_action(
        db, user, "annotation_deleted", "annotation", str(ann_id),
        details={"document_id": str(ann.document_id)},
    )
    await db.commit()
    return {"ok": True}
