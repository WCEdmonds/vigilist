from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids, get_user_role_for_production, ROLE_RANK
from app.models import Document, Redaction, RedactionQCDecision, User
from app.routers.auth import get_current_user
from app.schemas import (
    RedactionCreate,
    RedactionOut,
    RedactionQCDecisionCreate,
    RedactionQCDecisionOut,
    RedactionQCQueueItem,
    RedactionUpdate,
)
from app.services.audit import log_action
from app.services.privilege import qc_status
from app.services.redaction import is_valid_reason_code, validate_rect

router = APIRouter(prefix="/api", tags=["redactions"])


async def _load_accessible_doc(db: AsyncSession, user: User, doc_id: UUID) -> Document:
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    return doc


def _validate_or_422(page_num, x_pct, y_pct, w_pct, h_pct, page_count, reason_code):
    err = validate_rect(page_num, x_pct, y_pct, w_pct, h_pct, page_count)
    if err:
        raise HTTPException(status_code=422, detail=err)
    if not is_valid_reason_code(reason_code):
        raise HTTPException(status_code=422, detail="invalid reason_code")


@router.get("/documents/{doc_id}/redactions", response_model=list[RedactionOut])
async def list_redactions(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _load_accessible_doc(db, user, doc_id)
    result = await db.execute(
        select(Redaction)
        .where(Redaction.document_id == doc_id)
        .order_by(Redaction.page_num.asc(), Redaction.created_at.asc())
    )
    return [RedactionOut.model_validate(r) for r in result.scalars().all()]


@router.post("/documents/{doc_id}/redactions", response_model=RedactionOut, status_code=201)
async def create_redaction(
    doc_id: UUID,
    body: RedactionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = await _load_accessible_doc(db, user, doc_id)
    role = await get_user_role_for_production(db, user, doc.production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")

    _validate_or_422(body.page_num, body.x_pct, body.y_pct, body.w_pct, body.h_pct,
                     doc.page_count, body.reason_code)

    red = Redaction(
        document_id=doc_id,
        page_num=body.page_num,
        x_pct=body.x_pct,
        y_pct=body.y_pct,
        w_pct=body.w_pct,
        h_pct=body.h_pct,
        reason_code=body.reason_code,
        note=body.note,
        created_by=user.id,
    )
    db.add(red)
    await db.flush()
    await log_action(
        db, user, "redaction_created", "redaction", str(red.id),
        production_id=doc.production_id,
        details={"document_id": str(doc_id), "page_num": body.page_num, "reason_code": body.reason_code},
    )
    await db.commit()
    await db.refresh(red)
    return RedactionOut.model_validate(red)


async def _load_redaction_for_write(db: AsyncSession, user: User, redaction_id: int):
    accessible = await get_accessible_production_ids(db, user)
    red = await db.get(Redaction, redaction_id)
    if not red:
        raise HTTPException(status_code=404, detail="Redaction not found")
    doc = await db.get(Document, red.document_id)
    if not doc:
        # Orphaned redaction (parent gone) — shouldn't happen under the FK
        # CASCADE, but 404 is the correct semantics if it ever does.
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if red.created_by != user.id:
        role = await get_user_role_for_production(db, user, doc.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Only the creator or a manager can modify this redaction")
    return red, doc


@router.put("/redactions/{redaction_id}", response_model=RedactionOut)
async def update_redaction(
    redaction_id: int,
    body: RedactionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    red, doc = await _load_redaction_for_write(db, user, redaction_id)

    x = body.x_pct if body.x_pct is not None else red.x_pct
    y = body.y_pct if body.y_pct is not None else red.y_pct
    w = body.w_pct if body.w_pct is not None else red.w_pct
    h = body.h_pct if body.h_pct is not None else red.h_pct
    reason = body.reason_code if body.reason_code is not None else red.reason_code
    _validate_or_422(red.page_num, x, y, w, h, doc.page_count, reason)

    red.x_pct, red.y_pct, red.w_pct, red.h_pct = x, y, w, h
    red.reason_code = reason
    if body.note is not None:
        red.note = body.note

    await log_action(
        db, user, "redaction_updated", "redaction", str(redaction_id),
        details={"document_id": str(red.document_id)},
    )
    await db.commit()
    await db.refresh(red)
    return RedactionOut.model_validate(red)


@router.delete("/redactions/{redaction_id}")
async def delete_redaction(
    redaction_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    red, _doc = await _load_redaction_for_write(db, user, redaction_id)
    document_id = red.document_id
    await db.delete(red)
    await log_action(
        db, user, "redaction_deleted", "redaction", str(redaction_id),
        details={"document_id": str(document_id)},
    )
    await db.commit()
    return {"ok": True}


@router.post("/documents/{doc_id}/redaction-qc", response_model=RedactionQCDecisionOut, status_code=201)
async def decide_redaction_qc(
    doc_id: UUID,
    body: RedactionQCDecisionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = await _load_accessible_doc(db, user, doc_id)
    role = await get_user_role_for_production(db, user, doc.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    count = (await db.execute(
        select(func.count(Redaction.id)).where(Redaction.document_id == doc_id)
    )).scalar() or 0
    if count == 0:
        raise HTTPException(status_code=422, detail="Document has no redactions to QC")

    dec = RedactionQCDecision(
        document_id=doc_id,
        decision=body.decision,
        note=body.note,
        redaction_count=count,
        decided_by=user.id,
    )
    db.add(dec)
    await db.flush()
    await log_action(
        db, user, "redaction_qc_decided", "redaction_qc", str(dec.id),
        production_id=doc.production_id,
        details={"document_id": str(doc_id), "decision": body.decision,
                 "redaction_count": count},
    )
    await db.commit()
    await db.refresh(dec)
    return RedactionQCDecisionOut.model_validate(dec)


@router.get("/productions/{production_id}/redaction-qc", response_model=list[RedactionQCQueueItem])
async def redaction_qc_queue(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    agg = await db.execute(
        select(
            Document.id,
            Document.bates_begin,
            func.count(Redaction.id).label("cnt"),
            func.max(func.coalesce(Redaction.updated_at, Redaction.created_at)).label("changed"),
        )
        # INNER JOIN by design: the QC queue lists only documents that
        # currently have redactions — a doc with none has nothing to QC.
        # (not_applicable status surfaces via the privilege log instead.)
        .join(Redaction, Redaction.document_id == Document.id)
        .where(Document.production_id == production_id)
        .group_by(Document.id, Document.bates_begin)
        .order_by(Document.bates_begin)
    )
    rows = agg.all()

    latest: dict = {}
    doc_ids = [r[0] for r in rows]
    if doc_ids:
        dec_result = await db.execute(
            select(RedactionQCDecision)
            .where(RedactionQCDecision.document_id.in_(doc_ids))
            .order_by(RedactionQCDecision.document_id,
                      RedactionQCDecision.decided_at.desc(),
                      RedactionQCDecision.id.desc())
        )
        for d in dec_result.scalars().all():
            latest.setdefault(d.document_id, d)

    items = []
    for did, bates, cnt, changed in rows:
        d = latest.get(did)
        status = qc_status(cnt, (d.decision, d.decided_at, d.redaction_count) if d else None, changed)
        items.append(RedactionQCQueueItem(
            document_id=did,
            bates_begin=bates,
            redaction_count=cnt,
            qc_status=status,
            latest_decision=RedactionQCDecisionOut.model_validate(d) if d else None,
        ))
    return items
