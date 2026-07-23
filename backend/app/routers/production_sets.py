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


@router.post("/production-sets/{set_id}/documents")
async def add_documents(
    set_id: int,
    body: ProductionSetAddDocuments,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "draft":
        raise HTTPException(status_code=409, detail="Production set is locked")
    if not body.document_ids and body.tag_id is None:
        raise HTTPException(status_code=422, detail="Provide document_ids and/or tag_id")

    explicit = set(body.document_ids or [])
    candidates = set(explicit)
    if body.tag_id is not None:
        tag_rows = (await db.execute(
            select(DocumentTag.document_id)
            .join(Document, Document.id == DocumentTag.document_id)
            .where(DocumentTag.tag_id == body.tag_id,
                   Document.production_id == ps.production_id)
        )).all()
        candidates.update(r[0] for r in tag_rows)

    info_rows = []
    if candidates:
        info_rows = (await db.execute(
            select(Document.id, Document.production_id, Document.family_id)
            .where(Document.id.in_(candidates))
        )).all()
    found = {r[0] for r in info_rows}
    if (explicit - found) or any(r[1] != ps.production_id for r in info_rows):
        raise HTTPException(status_code=422, detail="Documents not found in this matter")

    families_added = 0
    if body.include_families:
        fams = {r[2] for r in info_rows if r[2]}
        if fams:
            fam_rows = (await db.execute(
                select(Document.id)
                .where(Document.production_id == ps.production_id,
                       Document.family_id.in_(fams))
            )).all()
            fam_ids = {r[0] for r in fam_rows}
            families_added = len(fam_ids - candidates)
            candidates |= fam_ids

    skipped_duplicates = 0
    if body.exclude_duplicates:
        dup_rows = (await db.execute(
            select(DocumentDuplicate.group_id, DocumentDuplicate.document_id,
                   Document.bates_begin)
            .join(DuplicateGroup, DuplicateGroup.id == DocumentDuplicate.group_id)
            .join(Document, Document.id == DocumentDuplicate.document_id)
            .where(DuplicateGroup.production_id == ps.production_id,
                   DuplicateGroup.type == "hash")
        )).all()
        groups: dict[int, list[tuple[str, object]]] = {}
        for gid, did, control in dup_rows:
            groups.setdefault(gid, []).append((control, did))
        for members in groups.values():
            primary = min(members)[1]  # lowest control number wins
            for _, did in members:
                if did in candidates and did != primary and did not in explicit:
                    candidates.discard(did)
                    skipped_duplicates += 1

    existing_rows = (await db.execute(
        select(ProductionSetItem.document_id)
        .where(ProductionSetItem.production_set_id == set_id)
    )).all()
    existing = {r[0] for r in existing_rows}
    to_add = candidates - existing
    for did in sorted(to_add, key=str):
        db.add(ProductionSetItem(production_set_id=set_id, document_id=did))

    summary = {"added": len(to_add), "skipped_existing": len(candidates & existing),
               "skipped_duplicates": skipped_duplicates, "families_added": families_added}
    await log_action(db, user, "production_set_documents_added", "production_set",
                     str(set_id), production_id=ps.production_id, details=summary)
    await db.commit()
    return summary


@router.delete("/production-sets/{set_id}/documents")
async def remove_documents(
    set_id: int,
    body: ProductionSetRemoveDocuments,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "draft":
        raise HTTPException(status_code=409, detail="Production set is locked")
    await db.execute(
        sa_delete(ProductionSetItem).where(
            ProductionSetItem.production_set_id == set_id,
            ProductionSetItem.document_id.in_(body.document_ids),
        )
    )
    await log_action(db, user, "production_set_documents_removed", "production_set",
                     str(set_id), production_id=ps.production_id,
                     details={"document_ids": [str(i) for i in body.document_ids]})
    await db.commit()
    # count of requested ids (fake sessions have no rowcount)
    return {"removed": len(body.document_ids)}


@router.post("/production-sets/{set_id}/lock", response_model=ProductionSetLockOut)
async def lock_production_set(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "draft":
        raise HTTPException(status_code=409, detail="Production set is already locked")

    items = (await db.execute(
        select(ProductionSetItem).where(ProductionSetItem.production_set_id == set_id)
    )).scalars().all()
    if not items:
        raise HTTPException(status_code=422, detail="Cannot lock an empty production set")
    doc_ids = [i.document_id for i in items]

    doc_rows = (await db.execute(
        select(Document.id, Document.bates_begin, Document.family_id,
               Document.custodian, Document.date_sent, Document.date_received,
               Document.page_count, Document.privilege_disposition)
        .where(Document.id.in_(doc_ids))
    )).all()

    priv_rows = (await db.execute(
        select(DocumentTag.document_id)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(Tag.is_privilege.is_(True), DocumentTag.document_id.in_(doc_ids))
    )).all()
    privileged = {r[0] for r in priv_rows}

    red_rows = (await db.execute(
        select(Redaction.document_id, func.count(Redaction.id))
        .where(Redaction.document_id.in_(doc_ids))
        .group_by(Redaction.document_id)
    )).all()
    red_counts = {r[0]: r[1] for r in red_rows}

    members: list[MemberInfo] = []
    meta: dict = {}  # document_id -> (disposition, pages)
    for did, control, family_id, custodian, date_sent, date_received, page_count, override in doc_rows:
        disposition = effective_disposition(
            has_privilege_tag=did in privileged,
            has_redactions=red_counts.get(did, 0) > 0,
            override=override,
        ) or "produce"
        meta[did] = (disposition, pages_for(disposition, page_count or 1))
        members.append(MemberInfo(
            document_id=did, control_number=control, family_id=family_id,
            custodian=custodian, doc_date=date_sent or date_received,
        ))

    ordered = order_members(members, ps.sort_key)
    assignments = assign_bates(
        [(m.document_id, meta[m.document_id][1]) for m in ordered],
        ps.prefix, ps.padding, ps.start_number,
    )
    items_by_doc = {i.document_id: i for i in items}
    for did, sort_order, begin, end in assignments:
        item = items_by_doc[did]
        item.sort_order = sort_order
        item.bates_begin = begin
        item.bates_end = end
        item.disposition, item.pages = meta[did]

    ps.status = "locked"
    ps.locked_by = user.id
    ps.locked_at = datetime.now(timezone.utc).replace(tzinfo=None)

    summary = {
        "doc_count": len(assignments),
        "page_count": sum(meta[d][1] for d in items_by_doc),
        "bates_begin": assignments[0][2],
        "bates_end": assignments[-1][3],
    }
    await log_action(db, user, "production_set_locked", "production_set", str(set_id),
                     production_id=ps.production_id, details=summary)
    await db.commit()
    return ProductionSetLockOut(**summary)
