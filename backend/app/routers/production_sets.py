"""Production sets: deliverable volumes with draft->lock Bates assignment (P2-1)."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import delete as sa_delete, func, select, update as sa_update
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
    ProductionSetLockRequest,
    ProductionSetMemberOut,
    ProductionSetOut,
    ProductionSetRemoveDocuments,
)
from app.services import tasks
from app.services.audit import log_action
from app.services.oidc import verify_cloud_tasks_request
from app.services.privilege import effective_disposition
from app.services.production_export import compute_manifest, package_set
from app.services.production_render import finalize_if_complete, render_batch
from app.services.production_validation import compute_conflicts
from app.services.storage import get_signed_url
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
        render_status="not_started", package_status="not_started",
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
    out.rendered_count = sum(1 for i in items if i.output_path)
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
            select(Document.id, Document.production_id, Document.family_id,
                   Document.source_type)
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

    skipped_received = 0
    if body.exclude_received:
        rec_rows = (await db.execute(
            select(Document.id)
            .where(Document.id.in_(candidates),
                   Document.source_type == "received")
        )).all()
        for (rdid,) in rec_rows:
            if rdid not in explicit:
                candidates.discard(rdid)
                skipped_received += 1

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
               "skipped_duplicates": skipped_duplicates, "families_added": families_added,
               "skipped_received": skipped_received}
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


@router.get("/production-sets/{set_id}/validation")
async def get_validation(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Relativity-style staging validation: conflicts that must be resolved
    or explicitly overridden before the set can be locked."""
    ps = await _load_set(db, user, set_id)
    items = (await db.execute(
        select(ProductionSetItem)
        .where(ProductionSetItem.production_set_id == set_id)
    )).scalars().all()
    return await compute_conflicts(db, ps, [i.document_id for i in items])


@router.post("/production-sets/{set_id}/lock", response_model=ProductionSetLockOut)
async def lock_production_set(
    set_id: int,
    body: ProductionSetLockRequest | None = None,
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

    conflicts = await compute_conflicts(db, ps, doc_ids)
    if conflicts["total"]:
        counts = {k: len(v) for k, v in conflicts.items() if k != "total"}
        if not (body and body.override_conflicts):
            summary = ", ".join(f"{k}={v}" for k, v in counts.items())
            raise HTTPException(
                status_code=409,
                detail=f"Validation conflicts: {summary}. Resolve them or lock with override_conflicts.")
        ps.conflicts_overridden_by = user.id
        ps.conflicts_overridden_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await log_action(db, user, "production_set_conflicts_overridden",
                         "production_set", str(set_id),
                         production_id=ps.production_id,
                         details={**counts, "total": conflicts["total"]})

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


RENDER_BATCH_SIZE = 25


@router.post("/production-sets/{set_id}/render")
async def render_production_set(
    set_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "locked":
        raise HTTPException(status_code=409, detail="Production set must be locked before rendering")
    if ps.render_status == "rendering":
        raise HTTPException(status_code=409, detail="Render already in progress")

    rows = (await db.execute(
        select(ProductionSetItem.document_id)
        .where(ProductionSetItem.production_set_id == set_id)
        .order_by(ProductionSetItem.sort_order)
    )).all()
    doc_ids = [r[0] for r in rows]
    if not doc_ids:
        raise HTTPException(status_code=422, detail="Production set has no members")

    # Re-render semantics: clear prior artifact markers, then rebuild all.
    await db.execute(
        sa_update(ProductionSetItem)
        .where(ProductionSetItem.production_set_id == set_id)
        .values(output_path=None)
    )
    ps.render_status = "rendering"
    ps.render_error = None
    ps.rendered_at = None
    batches = [doc_ids[i:i + RENDER_BATCH_SIZE]
               for i in range(0, len(doc_ids), RENDER_BATCH_SIZE)]
    await log_action(db, user, "production_set_render_started", "production_set",
                     str(set_id), production_id=ps.production_id,
                     details={"documents": len(doc_ids), "batches": len(batches)})
    await db.commit()

    if tasks.is_configured():
        for batch in batches:
            tasks.enqueue_render_batch(set_id, [str(d) for d in batch])
    else:
        background_tasks.add_task(_render_inline, set_id, batches)
    return {"documents": len(doc_ids), "batches": len(batches)}


async def _render_inline(set_id: int, batches):
    """Dev fallback: run all batches in-process on a fresh session."""
    from app.database import async_session

    async with async_session() as db:
        for batch in batches:
            await render_batch(db, set_id, batch)
        await finalize_if_complete(db, set_id)


@router.post("/production-sets/render-batch")
async def render_batch_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker endpoint — renders one batch of set documents.

    Always returns 200; render failures land in render_status='error'
    (Cloud Tasks retries non-2xx, which would loop a deterministic failure).
    """
    set_id = body.get("set_id")
    document_ids = body.get("document_ids")
    if set_id is None or not document_ids:
        raise HTTPException(status_code=400, detail="Missing required fields")
    n = await render_batch(db, int(set_id), [UUID(d) for d in document_ids])
    await finalize_if_complete(db, int(set_id))
    return {"rendered": n}


@router.get("/production-sets/{set_id}/documents/{document_id}/pdf")
async def get_produced_pdf(
    set_id: int,
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _load_set(db, user, set_id)
    items = (await db.execute(
        select(ProductionSetItem).where(
            ProductionSetItem.production_set_id == set_id,
            ProductionSetItem.document_id == document_id,
        )
    )).scalars().all()
    if not items or not items[0].output_path:
        raise HTTPException(status_code=404, detail="Rendered output not found")
    item = items[0]
    url = get_signed_url(
        item.output_path,
        response_disposition=f'attachment; filename="{item.bates_begin}.pdf"',
    )
    return RedirectResponse(url, status_code=307)


@router.get("/production-sets/{set_id}/manifest")
async def get_manifest(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    if ps.status != "locked":
        raise HTTPException(status_code=409, detail="Production set must be locked")
    items = (await db.execute(
        select(ProductionSetItem)
        .where(ProductionSetItem.production_set_id == set_id)
        .order_by(ProductionSetItem.sort_order)
    )).scalars().all()
    return compute_manifest(ps, items)


@router.post("/production-sets/{set_id}/package")
async def package_production_set(
    set_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "locked" or ps.render_status != "rendered":
        raise HTTPException(status_code=409, detail="Production set must be rendered before packaging")
    if ps.package_status == "packaging":
        raise HTTPException(status_code=409, detail="Packaging already in progress")
    count = (await db.execute(
        select(func.count(ProductionSetItem.id))
        .where(ProductionSetItem.production_set_id == set_id)
    )).scalar() or 0
    ps.package_status = "packaging"
    ps.package_error = None
    ps.package_path = None
    ps.packaged_at = None
    await log_action(db, user, "production_set_package_started", "production_set",
                     str(set_id), production_id=ps.production_id,
                     details={"documents": count})
    await db.commit()
    if tasks.is_configured():
        tasks.enqueue_package(set_id)
    else:
        background_tasks.add_task(_package_inline, set_id)
    return {"documents": count}


async def _package_inline(set_id: int):
    """Dev fallback: package in-process on a fresh session."""
    from app.database import async_session

    async with async_session() as db:
        await package_set(db, set_id)


@router.post("/production-sets/package-worker")
async def package_worker_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker — packages one set. Always 200; failures land in
    package_status='error' (non-2xx would loop a deterministic failure)."""
    set_id = body.get("set_id")
    if set_id is None:
        raise HTTPException(status_code=400, detail="set_id required")
    await package_set(db, int(set_id))
    return {"ok": True}


@router.get("/production-sets/{set_id}/package")
async def download_package(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    if ps.package_status != "packaged" or not ps.package_path:
        raise HTTPException(status_code=404, detail="Package not available")
    url = get_signed_url(
        ps.package_path,
        response_disposition=f'attachment; filename="{ps.prefix}_production.zip"',
    )
    return RedirectResponse(url, status_code=307)
