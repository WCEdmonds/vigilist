"""Production sets: deliverable volumes with draft->lock Bates assignment (P2-1)."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import (
    Document,
    DocumentDuplicate,
    DocumentTag,
    DuplicateGroup,
    ProductionSet,
    ProductionSetItem,
    Redaction,
    Tag,
    User,
)
from app.routers.auth import get_current_user
from app.schemas import (
    ProductionSetAddDocuments,
    ProductionSetCreate,
    ProductionSetLockOut,
    ProductionSetMemberOut,
    ProductionSetOut,
    ProductionSetRemoveDocuments,
)
from app.services.audit import log_action
from app.services.privilege import effective_disposition
from app.services.production_numbering import (
    SORT_KEYS,
    MemberInfo,
    assign_bates,
    order_members,
    pages_for,
)

router = APIRouter(prefix="/api", tags=["production-sets"])


async def _load_set(
    db: AsyncSession, user: User, set_id: int, require_manager: bool = False
) -> ProductionSet:
    ps = await db.get(ProductionSet, set_id)
    if not ps:
        raise HTTPException(status_code=404, detail="Production set not found")
    accessible = await get_accessible_production_ids(db, user)
    if ps.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if require_manager:
        role = await get_user_role_for_production(db, user, ps.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Manager or higher role required")
    return ps


@router.post("/productions/{production_id}/production-sets",
             response_model=ProductionSetOut, status_code=201)
async def create_production_set(
    production_id: int,
    body: ProductionSetCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")
    if not body.prefix or any(c.isspace() for c in body.prefix):
        raise HTTPException(status_code=422, detail="prefix must be non-empty with no whitespace")
    if body.sort_key not in SORT_KEYS:
        raise HTTPException(status_code=422, detail="invalid sort_key")
    if not (1 <= body.padding <= 12) or body.start_number < 1:
        raise HTTPException(status_code=422, detail="invalid padding or start_number")

    dup = (await db.execute(
        select(ProductionSet.id).where(
            ProductionSet.production_id == production_id,
            ProductionSet.name == body.name,
        )
    )).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="A production set with this name already exists")

    # Pass every column explicitly: Python-side Column defaults only apply at
    # flush, and the fake-session tests never flush against a real DB.
    ps = ProductionSet(
        production_id=production_id, name=body.name, status="draft",
        prefix=body.prefix, padding=body.padding, start_number=body.start_number,
        sort_key=body.sort_key, designation=body.designation, created_by=user.id,
    )
    db.add(ps)
    await db.flush()
    await log_action(db, user, "production_set_created", "production_set", str(ps.id),
                     production_id=production_id,
                     details={"name": body.name, "prefix": body.prefix})
    await db.commit()
    await db.refresh(ps)
    return ProductionSetOut.model_validate(ps)


@router.get("/productions/{production_id}/production-sets",
            response_model=list[ProductionSetOut])
async def list_production_sets(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    sets = (await db.execute(
        select(ProductionSet)
        .where(ProductionSet.production_id == production_id)
        .order_by(ProductionSet.created_at, ProductionSet.id)
    )).scalars().all()
    counts: dict[int, int] = {}
    if sets:
        rows = (await db.execute(
            select(ProductionSetItem.production_set_id, func.count(ProductionSetItem.id))
            .where(ProductionSetItem.production_set_id.in_([s.id for s in sets]))
            .group_by(ProductionSetItem.production_set_id)
        )).all()
        counts = {r[0]: r[1] for r in rows}
    out = []
    for s in sets:
        o = ProductionSetOut.model_validate(s)
        o.doc_count = counts.get(s.id, 0)
        out.append(o)
    return out


@router.get("/production-sets/{set_id}", response_model=ProductionSetOut)
async def get_production_set(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    items = (await db.execute(
        select(ProductionSetItem)
        .where(ProductionSetItem.production_set_id == set_id)
        .order_by(ProductionSetItem.sort_order)
    )).scalars().all()
    out = ProductionSetOut.model_validate(ps)
    out.doc_count = len(items)
    if ps.status == "locked" and items:
        out.page_count = sum(i.pages or 0 for i in items)
        out.bates_begin = items[0].bates_begin
        out.bates_end = items[-1].bates_end
    return out


@router.get("/production-sets/{set_id}/documents",
            response_model=list[ProductionSetMemberOut])
async def list_production_set_documents(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    rows = (await db.execute(
        select(ProductionSetItem, Document.bates_begin)
        .join(Document, Document.id == ProductionSetItem.document_id)
        .where(ProductionSetItem.production_set_id == set_id)
        .order_by(ProductionSetItem.sort_order, Document.bates_begin)
    )).all()
    return [
        ProductionSetMemberOut(
            document_id=item.document_id, control_number=control,
            sort_order=item.sort_order, bates_begin=item.bates_begin,
            bates_end=item.bates_end, pages=item.pages,
            disposition=item.disposition, designation=item.designation,
        )
        for item, control in rows
    ]


@router.delete("/production-sets/{set_id}")
async def delete_production_set(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "draft":
        raise HTTPException(status_code=409, detail="Locked production sets cannot be deleted")
    await log_action(db, user, "production_set_deleted", "production_set", str(set_id),
                     production_id=ps.production_id, details={"name": ps.name})
    await db.delete(ps)
    await db.commit()
    return {"ok": True}
