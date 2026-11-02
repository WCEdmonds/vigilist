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
from app.services.families import is_attachment
from app.services.imaging import imaging_kind
from app.services.privilege import effective_disposition, qc_status
from app.services.redacted_metadata import blanked_fields

# Conflicts no override can wave through: lock must wait for them to clear.
NON_OVERRIDABLE = frozenset({"needs_pages"})


async def compute_conflicts(db: AsyncSession, ps: ProductionSet,
                            doc_ids: list) -> dict:
    out: dict = {"qc_pending": [], "privilege_produce": [], "no_images": [],
                 "received_document": [], "needs_pages": [], "redactions_overridden": [],
                 "attachment_names_redacted": [], "total": 0}
    if not doc_ids:
        return out

    doc_rows = (await db.execute(
        select(Document.id, Document.bates_begin,
               Document.privilege_disposition, Document.image_paths,
               Document.source_type, Document.source_party)
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

    # Imaging state + family, kept out of doc_rows' fixed tuple.
    info = {r[0]: r[1:] for r in (await db.execute(
        select(Document.id, Document.file_type, Document.native_path,
               Document.imaging_status, Document.family_id, Document.header_regions)
        .where(Document.id.in_(doc_ids))
    )).all()}
    families = {fam for (_ft, _np, _st, fam, _hr) in info.values() if fam}
    parents: dict = {}
    if families:
        parents = {bates: (pid, regions) for pid, bates, regions in (await db.execute(
            select(Document.id, Document.bates_begin, Document.header_regions)
            .where(Document.production_id == ps.production_id,
                   Document.bates_begin.in_(families),
                   Document.header_regions.is_not(None))
        )).all()}
    names_redacted: set = set()
    if parents:
        parent_reds: dict = {}
        for red in (await db.execute(
            select(Redaction).where(Redaction.document_id.in_([p for p, _ in parents.values()]))
        )).scalars().all():
            parent_reds.setdefault(red.document_id, []).append(red)
        names_redacted = {bates for bates, (pid, regions) in parents.items()
                          if "attachments" in blanked_fields(regions, parent_reds.get(pid, []))}

    latest: dict = {}
    for d in (await db.execute(
        select(RedactionQCDecision)
        .where(RedactionQCDecision.document_id.in_(doc_ids))
        .order_by(RedactionQCDecision.document_id,
                  RedactionQCDecision.decided_at.desc(),
                  RedactionQCDecision.id.desc())
    )).scalars().all():
        latest.setdefault(d.document_id, d)

    for did, control, override, image_paths, source_type, source_party in doc_rows:
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
        file_type, native_path, imaging_status, family_id, _hr = info.get(
            did, (None, None, None, None, None))
        # Same predicate as lock: produce + native + chosen type ships natively.
        ships_native = bool(disposition == "produce" and native_path
                            and file_type in (getattr(ps, "native_file_types", None) or []))
        if disposition != "withhold" and not image_paths and not ships_native:
            probe = type("D", (), dict(image_paths=[], file_type=file_type, native_path=native_path))
            if imaging_kind(probe) is not None or imaging_status in ("queued", "running"):
                out["needs_pages"].append({
                    "document_id": str(did), "control_number": control,
                    "detail": "pages must be prepared before lock"})
            else:
                out["no_images"].append({
                    "document_id": str(did), "control_number": control,
                    "detail": "no page images — Word/Excel/PowerPoint can't be imaged yet"})
        if count and override == "produce":
            out["redactions_overridden"].append({
                "document_id": str(did), "control_number": control,
                "detail": "has redactions but is set to Produce (would ship unredacted)"})
        if (disposition != "withhold" and family_id and is_attachment(control, family_id)
                and family_id in names_redacted):
            out["attachment_names_redacted"].append({
                "document_id": str(did), "control_number": control,
                "detail": "attachment names are redacted on the parent email, "
                          "but this attachment ships its filename"})
        if source_type == "received":
            out["received_document"].append({
                "document_id": str(did), "control_number": control,
                "detail": f"received from {source_party or 'another party'} — "
                          "not part of our collection"})

    out["total"] = sum(len(v) for k, v in out.items() if k != "total")
    return out
