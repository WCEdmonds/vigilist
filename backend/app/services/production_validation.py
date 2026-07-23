"""Pre-lock validation for production sets (P2-3.5). DB-aware.

Adapted from Relativity's staging validation: conflicts are surfaced and
must be resolved or explicitly overridden — never silently produced.
Dispositions are derived live so validation works on draft sets.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Document,
    DocumentTag,
    ProductionSet,
    Redaction,
    RedactionQCDecision,
    Tag,
)
from app.services.privilege import effective_disposition, qc_status


async def compute_conflicts(db: AsyncSession, ps: ProductionSet,
                            doc_ids: list) -> dict:
    out: dict = {"qc_pending": [], "privilege_produce": [], "no_images": [],
                 "total": 0}
    if not doc_ids:
        return out

    doc_rows = (await db.execute(
        select(Document.id, Document.bates_begin,
               Document.privilege_disposition, Document.image_paths)
        .where(Document.id.in_(doc_ids))
    )).all()

    privileged = {r[0] for r in (await db.execute(
        select(DocumentTag.document_id)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(Tag.is_privilege.is_(True), DocumentTag.document_id.in_(doc_ids))
    )).all()}

    red_rows = (await db.execute(
        select(Redaction.document_id, func.count(Redaction.id),
               func.max(func.coalesce(Redaction.updated_at, Redaction.created_at)))
        .where(Redaction.document_id.in_(doc_ids))
        .group_by(Redaction.document_id)
    )).all()
    reds = {r[0]: (r[1], r[2]) for r in red_rows}

    latest: dict = {}
    for d in (await db.execute(
        select(RedactionQCDecision)
        .where(RedactionQCDecision.document_id.in_(doc_ids))
        .order_by(RedactionQCDecision.document_id,
                  RedactionQCDecision.decided_at.desc(),
                  RedactionQCDecision.id.desc())
    )).scalars().all():
        latest.setdefault(d.document_id, d)

    for did, control, override, image_paths in doc_rows:
        count, changed = reds.get(did, (0, None))
        disposition = effective_disposition(
            has_privilege_tag=did in privileged,
            has_redactions=count > 0,
            override=override,
        ) or "produce"
        if disposition == "redact_in_part":
            dec = latest.get(did)
            status = qc_status(
                count,
                (dec.decision, dec.decided_at, dec.redaction_count) if dec else None,
                changed,
            )
            if status != "approved":
                out["qc_pending"].append({
                    "document_id": str(did), "control_number": control,
                    "detail": f"redaction QC is {status}"})
        if did in privileged and disposition == "produce":
            out["privilege_produce"].append({
                "document_id": str(did), "control_number": control,
                "detail": "privilege-tagged document would be produced unredacted"})
        if disposition != "withhold" and not image_paths:
            out["no_images"].append({
                "document_id": str(did), "control_number": control,
                "detail": "no page images to produce"})

    out["total"] = (len(out["qc_pending"]) + len(out["privilege_produce"])
                    + len(out["no_images"]))
    return out
