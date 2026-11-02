"""Prepare pages: image documents for redaction, one at a time or in bulk.

Imaging runs on the worker via Cloud Tasks (never on the web service); the
viewer and the production builder poll the document's imaging_status.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import Document, Redaction, User
from app.routers.auth import get_current_user
from app.schemas import FolderAddDocuments as PreparePagesRequest
from app.services import bulk_tagging as bt
from app.services import tasks
from app.services.audit import log_action
from app.services.imaging import ImagingRefused, image_document, imaging_kind
from app.services.oidc import verify_cloud_tasks_request
from app.services.sql_arrays import ids_match

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["imaging"])

# A queued/running job older than this is treated as dead and may be re-queued
# (Cloud Tasks' dispatch deadline is 30 minutes).
STALE_IMAGING = timedelta(minutes=35)
_IN_PROGRESS = ("queued", "running")
_local_tasks: set = set()


def _now():
    return datetime.now(timezone.utc)


def _in_progress(doc) -> bool:
    if doc.imaging_status not in _IN_PROGRESS:
        return False
    started = doc.imaging_requested_at
    if started is None:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return _now() - started < STALE_IMAGING


async def _run_local(ids: list[str], user_id: str) -> None:
    async with async_session() as db:
        await _image_all(db, ids, user_id)


def _dispatch(ids: list[str], user_id: str) -> None:
    if tasks.is_configured():
        tasks.enqueue_imaging(ids, user_id)
    else:  # local development: no Cloud Tasks
        task = asyncio.create_task(_run_local(ids, user_id))
        _local_tasks.add(task)
        task.add_done_callback(_local_tasks.discard)


async def _require_writer(db, user, production_id: int) -> None:
    if production_id not in await get_accessible_production_ids(db, user):
        raise HTTPException(status_code=404, detail="Document not found")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["reviewer"]:
        raise HTTPException(status_code=403, detail="Read-only access")


def _queue(doc) -> None:
    doc.imaging_status = "queued"
    doc.imaging_error = None
    doc.imaging_requested_at = _now()


@router.post("/documents/{doc_id}/prepare-pages")
async def prepare_pages(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    await _require_writer(db, user, doc.production_id)
    redactions = (await db.execute(
        select(func.count(Redaction.id)).where(Redaction.document_id == doc_id)
    )).scalar() or 0
    if redactions:
        raise HTTPException(status_code=409, detail="This document has redactions; its pages are frozen")
    if doc.image_paths:
        raise HTTPException(status_code=422, detail="Already has page images")
    if imaging_kind(doc) is None:
        raise HTTPException(status_code=422, detail="Word, Excel and PowerPoint files can't be imaged yet")
    if _in_progress(doc):
        return {"imaging_status": doc.imaging_status}

    _queue(doc)
    await log_action(db, user, "document_imaging_requested", "document", str(doc_id),
                     production_id=doc.production_id, details={"queued": 1})
    # Commit before dispatch: the worker must see the queued row.
    await db.commit()
    try:
        _dispatch([str(doc_id)], user.id)
    except Exception as e:
        logger.exception("Could not dispatch imaging for %s", doc_id)
        doc.imaging_status = "failed"
        doc.imaging_error = f"could not start: {e}"[:500]
        await db.commit()
    return {"imaging_status": doc.imaging_status}


@router.post("/productions/{production_id}/prepare-pages")
async def prepare_pages_bulk(
    production_id: int,
    body: PreparePagesRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require_writer(db, user, production_id)
    if body.query is not None:
        query = body.query.model_copy(update={"production_id": production_id})
        try:
            matched = await bt.resolve_query_docs(db, query, [production_id])
        except bt.NoCriteriaError:
            raise HTTPException(status_code=422, detail="Search has no text or filters")
    else:
        matched = await bt.resolve_explicit_docs(db, body.doc_ids)
        if any(pid != production_id for pid in matched.values()):
            raise HTTPException(status_code=422, detail="Documents must be from this matter")
    targets = await bt.expand_attachments(db, matched) if body.include_attachments else dict(matched)
    if len(targets) > bt.MAX_BULK_DOCS:
        raise HTTPException(status_code=422, detail=f"More than {bt.MAX_BULK_DOCS:,} documents — narrow the search")

    ids = list(targets)
    counts = {"queued": 0, "already_imaged": 0, "in_progress": 0, "unsupported": 0, "has_redactions": 0}
    if not ids:
        return counts
    redacted = {d for d, n in (await db.execute(
        select(Redaction.document_id, func.count(Redaction.id))
        .where(ids_match(Redaction.document_id, ids))
        .group_by(Redaction.document_id)
    )).all() if n}
    docs = (await db.execute(select(Document).where(ids_match(Document.id, ids)))).scalars().all()
    to_queue = []
    for doc in docs:
        if doc.image_paths:
            counts["already_imaged"] += 1
        elif doc.id in redacted:
            counts["has_redactions"] += 1
        elif imaging_kind(doc) is None:
            counts["unsupported"] += 1
        elif _in_progress(doc):
            counts["in_progress"] += 1
        else:
            _queue(doc)
            to_queue.append(str(doc.id))
    counts["queued"] = len(to_queue)
    await log_action(db, user, "document_imaging_requested", "production", str(production_id),
                     production_id=production_id, details=counts)
    await db.commit()
    if to_queue:
        _dispatch(to_queue, user.id)
    return counts


async def _image_all(db, ids: list[str], user_id: str | None) -> int:
    user = await db.get(User, user_id) if user_id else None
    for raw in ids:
        doc_id = UUID(raw)
        try:
            await image_document(db, doc_id)
        except ImagingRefused:
            pass
        except Exception:
            logger.exception("Imaging crashed for %s", raw)
            continue
        if user is not None:
            doc = await db.get(Document, doc_id)
            if doc is not None:
                ok = doc.imaging_status == "done"
                await log_action(db, user, "document_imaged" if ok else "document_imaging_failed",
                                 "document", raw, production_id=doc.production_id,
                                 details={"pages": doc.page_count, "from": doc.imaged_from}
                                 if ok else {"error": doc.imaging_error})
                await db.commit()
    return len(ids)


@router.post("/documents/imaging-worker")
async def imaging_worker(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker: image each document in turn. Always 200 — failures
    are recorded on the document (a retry loop would repeat them)."""
    ids = body.get("document_ids") or []
    return {"processed": await _image_all(db, ids, body.get("requested_by"))}
