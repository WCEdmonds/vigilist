"""Assemble privilege-log rows. DB-aware; pure logic lives in privilege.py."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentTag, Redaction, RedactionQCDecision, Tag
from app.services.privilege import effective_disposition, log_description, qc_status
from app.services.redaction_render import REASON_LABELS


async def build_privilege_log_rows(db: AsyncSession, production_id: int) -> list[dict]:
    tagged = (await db.execute(
        select(Document.id, Tag.name)
        .join(DocumentTag, DocumentTag.document_id == Document.id)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(Document.production_id == production_id, Tag.is_privilege.is_(True))
    )).all()
    tag_names: dict = {}
    for did, name in tagged:
        tag_names.setdefault(did, set()).add(name)

    red_rows = (await db.execute(
        select(Redaction)
        .join(Document, Document.id == Redaction.document_id)
        .where(Document.production_id == production_id)
    )).scalars().all()
    reds: dict = {}
    for r in red_rows:
        reds.setdefault(r.document_id, []).append(r)

    override_docs = (await db.execute(
        select(Document).where(
            Document.production_id == production_id,
            Document.privilege_disposition.is_not(None),
        )
    )).scalars().all()

    candidate_ids = set(tag_names) | set(reds) | {d.id for d in override_docs}
    if not candidate_ids:
        return []

    docs = (await db.execute(
        select(Document).where(
            Document.production_id == production_id,
            Document.id.in_(candidate_ids),
        )
    )).scalars().all()

    decisions = (await db.execute(
        select(RedactionQCDecision)
        .where(RedactionQCDecision.document_id.in_(candidate_ids))
        .order_by(RedactionQCDecision.document_id,
                  RedactionQCDecision.decided_at.desc(),
                  RedactionQCDecision.id.desc())
    )).scalars().all()
    latest: dict = {}
    for d in decisions:
        latest.setdefault(d.document_id, d)

    rows = []
    for doc in sorted(docs, key=lambda d: d.bates_begin):
        doc_reds = reds.get(doc.id, [])
        disposition = effective_disposition(
            has_privilege_tag=doc.id in tag_names,
            has_redactions=bool(doc_reds),
            override=doc.privilege_disposition,
        )
        if disposition in (None, "produce"):
            continue

        basis = set(tag_names.get(doc.id, set()))
        if doc_reds:
            basis.update(REASON_LABELS.get(r.reason_code, "REDACTED") for r in doc_reds)
        basis_list = sorted(basis)

        changed = None
        if doc_reds:
            changed = max((r.updated_at or r.created_at) for r in doc_reds)
        dec = latest.get(doc.id)
        status = qc_status(
            len(doc_reds),
            (dec.decision, dec.decided_at, dec.redaction_count) if dec else None,
            changed,
        )

        doc_date = doc.date_sent or doc.date_received
        rows.append({
            "document_id": str(doc.id),
            "bates_begin": doc.bates_begin,
            "bates_end": doc.bates_end,
            "doc_date": doc_date.date().isoformat() if doc_date else None,
            "custodian": doc.custodian,
            "author": doc.email_from,
            "recipients": doc.email_to,
            "file_type": doc.file_type,
            "disposition": disposition,
            "basis": basis_list,
            "description": log_description(
                doc.email_from, doc.email_to, doc_date, doc.file_type,
                basis_list, disposition, doc.privilege_description,
            ),
            "qc_status": status,
        })
    return rows
