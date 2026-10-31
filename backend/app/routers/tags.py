from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Document, DocumentTag, Tag, User
from app.routers.auth import get_current_user
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.services import bulk_tagging as bt
from app.services.audit import log_action
from app.services.privilege import DISPOSITIONS
from app.services.sql_arrays import ids_match
from app.schemas import (
    ApplyTagsRequest,
    BulkTagByQueryRequest,
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

    if body.family and doc.family_id:
        await _apply_to_family(db, user, doc, body.tag_ids)
        await db.commit()
        return await _document_tags(db, doc_id)

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
    return await _document_tags(db, doc_id)


async def _document_tags(db, doc_id):
    result = await db.execute(
        select(DocumentTag)
        .where(DocumentTag.document_id == doc_id)
        .options(selectinload(DocumentTag.tag))
    )
    return result.scalars().all()


async def _family_ids(db, doc) -> list[UUID]:
    """The document plus every member of its family, same production."""
    return list(await bt.expand_attachments(db, {doc.id: doc.production_id}))


async def _apply_to_family(db, user, doc, tag_ids):
    """Code the parent and its attachments together. A conflicting
    responsiveness tag on a member is switched, so the family never ends up
    half Responsive, half Not Responsive."""
    ids = await _family_ids(db, doc)
    for tag_id in tag_ids:
        tag = await db.get(Tag, tag_id)
        if not tag:
            raise HTTPException(status_code=404, detail="Tag not found")
        if tag.production_id is not None and tag.production_id != doc.production_id:
            raise HTTPException(status_code=422, detail="Tag belongs to a different production")
        already = await bt.already_tagged_ids(db, tag_id, ids)
        conflicts, _ = await bt.conflicting_tags(db, tag, ids)
        plan = bt.plan_tagging(ids, already, conflicts, "switch")
        await bt.apply_plan(db, plan, tag_id, user.id, {})
        for member in sorted(plan.to_tag, key=str):
            details = {"tag_id": tag_id, "family": True, "source_document_id": str(doc.id)}
            if member in plan.switch:
                details["replaced_tag_ids"] = sorted(plan.switch[member])
            await log_action(db, user, "tag_applied", "document", str(member),
                             production_id=doc.production_id, details=details)


@router.delete("/documents/{doc_id}/tags/{tag_id}")
async def remove_tag(
    doc_id: UUID,
    tag_id: int,
    family: bool = False,
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
    if family and doc.family_id:
        ids = await _family_ids(db, doc)
        tagged = [r[0] for r in (await db.execute(
            select(DocumentTag.document_id)
            .where(DocumentTag.tag_id == tag_id, ids_match(DocumentTag.document_id, ids))
        )).all()]
        if not tagged:
            raise HTTPException(status_code=404, detail="Tag not applied")
        await db.execute(
            delete(DocumentTag)
            .where(DocumentTag.tag_id == tag_id, ids_match(DocumentTag.document_id, tagged))
            .execution_options(synchronize_session=False)
        )
        for member in tagged:
            await log_action(db, user, "tag_removed", "document", str(member),
                             production_id=doc.production_id,
                             details={"tag_id": tag_id, "family": True,
                                      "source_document_id": str(doc_id)})
        await db.commit()
        return {"ok": True, "removed": len(tagged)}
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


@router.post("/documents/bulk-tag-by-query")
async def bulk_tag_by_query(
    body: BulkTagByQueryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Apply one tag to every document matching a search (or to explicit
    IDs), plus attachments. dry_run returns the counts the confirm step shows;
    conflicting responsiveness tags must be resolved with conflict_action."""
    tag = await db.get(Tag, body.tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    override: dict = {}
    if body.disposition is not None:
        if body.disposition not in DISPOSITIONS:
            raise HTTPException(status_code=422, detail="invalid disposition")
        override["privilege_disposition"] = body.disposition
    if body.description is not None and body.description.strip():
        override["privilege_description"] = body.description.strip()
    if override and not tag.is_privilege:
        raise HTTPException(
            status_code=422,
            detail="Privilege disposition and description only apply with a privilege tag")

    accessible = await get_accessible_production_ids(db, user)
    if body.query is not None:
        try:
            matched = await bt.resolve_query_docs(db, body.query, accessible)
        except bt.NoCriteriaError:
            raise HTTPException(status_code=422, detail="Search has no text or filters")
    else:
        matched = await bt.resolve_explicit_docs(db, body.doc_ids)
        missing = set(body.doc_ids) - set(matched)
        if missing:
            raise HTTPException(status_code=404, detail=f"Document {next(iter(missing))} not found")
        if any(pid not in accessible for pid in matched.values()):
            raise HTTPException(status_code=403, detail="Access denied")

    targets = await bt.expand_attachments(db, matched) if body.include_attachments else dict(matched)
    if len(targets) > bt.MAX_BULK_DOCS:
        raise HTTPException(
            status_code=422,
            detail=f"More than {bt.MAX_BULK_DOCS:,} documents — narrow the search")

    productions = set(targets.values())
    if tag.production_id is not None and productions - {tag.production_id}:
        raise HTTPException(status_code=422, detail="Tag belongs to a different production")
    required = "manager" if override else "reviewer"
    for pid in sorted(productions):
        role = await get_user_role_for_production(db, user, pid)
        if ROLE_RANK.get(role, 0) < ROLE_RANK[required]:
            raise HTTPException(
                status_code=403,
                detail="Manager or higher role required" if override else "Read-only access")

    ids = list(targets)
    already = await bt.already_tagged_ids(db, tag.id, ids)
    conflicts, conflict_names = await bt.conflicting_tags(db, tag, ids)
    plan = bt.plan_tagging(targets.keys(), already, conflicts, body.conflict_action)
    prior = await bt.existing_overrides(db, list(plan.coded)) if tag.is_privilege else {}

    if body.dry_run:
        return {
            "matched": len(matched),
            "attachments_added": len(targets) - len(matched),
            "total": len(targets),
            "already_tagged": len(plan.already_tagged),
            "conflicts": len(plan.conflicts),
            "conflict_tags": bt.conflict_tag_counts(plan.conflicts, conflict_names),
            "existing_overrides": len(prior),
        }
    if plan.conflicts and body.conflict_action is None:
        raise HTTPException(status_code=409, detail="conflicts_unresolved")

    # Hand-written privilege log entries are kept unless the reviewer chose to
    # overwrite them; when they do, the old values go into the audit trail.
    override_ids = plan.coded if body.overwrite_existing else plan.coded - prior.keys()
    skipped = len(plan.coded) - len(override_ids) if override else 0
    overridden = await bt.apply_plan(db, plan, tag.id, user.id, override, override_ids)

    criteria = (body.query.model_dump() if body.query is not None
                else {"doc_ids": [str(d) for d in body.doc_ids]})
    for pid in sorted(productions):
        in_prod = {d for d, p in targets.items() if p == pid}
        await log_action(
            db, user, "bulk_tag_by_query", "document", None,
            production_id=pid,
            details={
                "tag_id": tag.id,
                "tag_name": tag.name,
                "tagged": len(plan.to_tag & in_prod),
                "switched": len(set(plan.switch) & in_prod),
                "left": len(plan.left & in_prod),
                "conflict_action": body.conflict_action,
                "include_attachments": body.include_attachments,
                "criteria": criteria,
                "override": override,
                "skipped_existing_overrides": (
                    len((plan.coded & in_prod) - override_ids) if override else 0),
                **({"previous_overrides": {
                    str(d): {"disposition": disp, "description": desc}
                    for d, (disp, desc) in prior.items() if d in in_prod}}
                   if override and body.overwrite_existing and prior else {}),
            },
        )
    await db.commit()
    return {
        "total": len(targets),
        "tagged": len(plan.to_tag),
        "already_tagged": len(plan.already_tagged),
        "switched": len(plan.switch),
        "left": len(plan.left),
        "overridden": overridden,
        "skipped_existing_overrides": skipped,
    }
