"""Chain of custody, lineage, and exceptions reports (P3-4). Read-only.

Pure assembly: the audit log, ingest jobs, hashes, review results, and
production sets already hold the record — these endpoints read it back.
"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids
from app.models import (
    AuditLog,
    Document,
    DocumentTag,
    IngestJob,
    ProductionSet,
    ProductionSetItem,
    Redaction,
    RedactionQCDecision,
    Tag,
    TarValidationReport,
    User,
)
from app.models_review import AIReviewResult, ReviewProject
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api", tags=["defensibility"])


async def _check_production_access(db, user, production_id: int):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("/documents/{doc_id}/lineage")
async def document_lineage(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await _check_production_access(db, user, doc.production_id)

    tag_rows = (await db.execute(
        select(Tag.name, DocumentTag.applied_by, DocumentTag.applied_at)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_id == doc.id)
    )).all()

    review_rows = (await db.execute(
        select(AIReviewResult).where(AIReviewResult.document_id == doc.id)
    )).scalars().all()

    qc_rows = (await db.execute(
        select(RedactionQCDecision)
        .where(RedactionQCDecision.document_id == doc.id)
        .order_by(RedactionQCDecision.decided_at)
    )).scalars().all()

    redaction_count = (await db.execute(
        select(func.count(Redaction.id)).where(Redaction.document_id == doc.id)
    )).scalar() or 0

    prod_rows = (await db.execute(
        select(ProductionSetItem, ProductionSet.name, ProductionSet.status)
        .join(ProductionSet, ProductionSet.id == ProductionSetItem.production_set_id)
        .where(ProductionSetItem.document_id == doc.id)
    )).all()

    audit_rows = (await db.execute(
        select(AuditLog).where(AuditLog.resource_id == str(doc.id))
        .order_by(AuditLog.created_at.desc()).limit(200)
    )).scalars().all()

    return {
        "identity": {
            "document_id": str(doc.id),
            "control_number": doc.bates_begin,
            "source_party": doc.source_party,
            "source_type": doc.source_type,
            "source_path": doc.source_path,
            "file_name": doc.file_name,
            "file_type": doc.file_type,
            "custodian": doc.custodian,
            "md5": doc.file_hash_md5,
            "sha256": doc.file_hash_sha256,
            "extraction_status": doc.extraction_status,
            "extraction_error": doc.extraction_error,
        },
        "tags": [{"name": n, "applied_by": b, "applied_at": a.isoformat() if a else None}
                 for n, b, a in tag_rows],
        "review": [{"project_id": r.project_id, "ai_decision": r.ai_decision,
                    "confidence": r.confidence_score,
                    "attorney_decision": r.attorney_decision,
                    "at": r.created_at.isoformat() if r.created_at else None}
                   for r in review_rows],
        "redactions": {
            "count": redaction_count,
            "qc_decisions": [{"decision": q.decision, "decided_by": q.decided_by,
                              "at": q.decided_at.isoformat() if q.decided_at else None}
                             for q in qc_rows],
        },
        "productions": [{"set_name": name, "set_status": status,
                         "bates_begin": item.bates_begin, "bates_end": item.bates_end,
                         "disposition": item.disposition,
                         "produce_native": bool(getattr(item, "produce_native", False)),
                         "output_path": item.output_path}
                        for item, name, status in prod_rows],
        "audit": [{"action": a.action, "user": a.user_email,
                   "at": a.created_at.isoformat() if a.created_at else None,
                   "details": a.details}
                  for a in audit_rows],
    }


async def _exceptions_rows(db, production_id: int):
    rows = (await db.execute(
        select(Document.id, Document.bates_begin, Document.file_name,
               Document.source_party, Document.extraction_status,
               Document.extraction_error)
        .where(Document.production_id == production_id,
               Document.extraction_status != "ok")
        .order_by(Document.bates_begin)
    )).all()
    counts: dict[str, int] = {}
    out = []
    for did, control, fname, party, status, err in rows:
        counts[status] = counts.get(status, 0) + 1
        out.append({"document_id": str(did), "control_number": control,
                    "file_name": fname, "source_party": party,
                    "extraction_status": status, "extraction_error": err})
    return out, counts


@router.get("/productions/{production_id}/exceptions")
async def exceptions_report(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _check_production_access(db, user, production_id)
    exceptions, counts = await _exceptions_rows(db, production_id)
    return {"total": len(exceptions), "counts": counts, "exceptions": exceptions}


@router.get("/productions/{production_id}/exceptions/csv")
async def exceptions_csv(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _check_production_access(db, user, production_id)
    exceptions, _counts = await _exceptions_rows(db, production_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Control Number", "File Name", "Source Party", "Status", "Error"])
    for e in exceptions:
        writer.writerow([e["control_number"], e["file_name"] or "",
                         e["source_party"] or "", e["extraction_status"],
                         e["extraction_error"] or ""])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=exceptions_report.csv"},
    )


@router.get("/productions/{production_id}/chain-of-custody")
async def chain_of_custody(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _check_production_access(db, user, production_id)

    jobs = (await db.execute(
        select(IngestJob).where(IngestJob.production_id == production_id)
        .order_by(IngestJob.created_at)
    )).scalars().all()

    by_source = dict((await db.execute(
        select(Document.source_type, func.count(Document.id))
        .where(Document.production_id == production_id)
        .group_by(Document.source_type)
    )).all())
    by_extraction = dict((await db.execute(
        select(Document.extraction_status, func.count(Document.id))
        .where(Document.production_id == production_id)
        .group_by(Document.extraction_status)
    )).all())
    hashed = (await db.execute(
        select(func.count(Document.id))
        .where(Document.production_id == production_id,
               Document.file_hash_sha256.is_not(None))
    )).scalar() or 0
    total = (await db.execute(
        select(func.count(Document.id))
        .where(Document.production_id == production_id)
    )).scalar() or 0

    projects = (await db.execute(
        select(ReviewProject).where(ReviewProject.production_id == production_id)
    )).scalars().all()
    validations = (await db.execute(
        select(TarValidationReport)
        .where(TarValidationReport.production_id == production_id)
        .order_by(TarValidationReport.created_at.desc())
    )).scalars().all()
    latest_validation_by_project: dict[int, dict] = {}
    for v in validations:
        if v.project_id not in latest_validation_by_project:
            control = (v.results or {}).get("control") or {}
            elusion = (v.results or {}).get("elusion") or {}
            latest_validation_by_project[v.project_id] = {
                "report_id": v.id,
                "recall": (control.get("recall") or {}).get("rate"),
                "precision": (control.get("precision") or {}).get("rate"),
                "elusion_rate": elusion.get("rate"),
            }

    sets = (await db.execute(
        select(ProductionSet).where(ProductionSet.production_id == production_id)
        .order_by(ProductionSet.created_at)
    )).scalars().all()

    return {
        "loads": [{"job_id": str(j.id), "source_format": j.source_format,
                   "status": j.status, "total_files": j.total_files,
                   "processed_files": j.processed_files,
                   "skipped_files": j.skipped_files,
                   "source_party": (j.field_mapping or {}).get("source_party"),
                   "source_type": (j.field_mapping or {}).get("source_type"),
                   "created_at": j.created_at.isoformat() if j.created_at else None,
                   "completed_at": j.completed_at.isoformat() if j.completed_at else None}
                  for j in jobs],
        "documents": {"total": total, "hashed_sha256": hashed,
                      "by_source_type": {str(k): v for k, v in by_source.items()},
                      "by_extraction_status": {str(k): v for k, v in by_extraction.items()}},
        "review": [{"project_id": p.id, "name": p.name, "status": p.status,
                    "processed_documents": p.processed_documents,
                    "total_documents": p.total_documents,
                    "validation": latest_validation_by_project.get(p.id)}
                   for p in projects],
        "productions": [{"set_id": s.id, "name": s.name, "status": s.status,
                         "prefix": s.prefix,
                         "render_status": s.render_status,
                         "package_status": s.package_status,
                         "packaged_at": s.packaged_at.isoformat() if s.packaged_at else None,
                         "conflicts_overridden_by": s.conflicts_overridden_by}
                        for s in sets],
    }
