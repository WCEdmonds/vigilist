"""Export endpoints: CSV export of document metadata, tags, and notes."""

import csv
import io
import json
import re
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Document, DocumentTag, Note, User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids
from app.services.metadata_report import METADATA_HEADERS, metadata_row
from app.services.privilege_log import build_privilege_log_rows
from app.services.sql_arrays import ids_match

router = APIRouter(prefix="/api/export", tags=["export"])

# ts_headline wraps matches in <mark>; a spreadsheet shows the raw tags.
_HIGHLIGHT = re.compile(r"</?mark>")


@router.get("/documents/csv")
async def export_documents_csv(
    production_id: int | None = None,
    tag_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Export documents as CSV with metadata, tags, and note counts."""
    accessible = await get_accessible_production_ids(db, user)
    query = (
        select(Document)
        .options(selectinload(Document.tags).selectinload(DocumentTag.tag))
        .where(Document.production_id.in_(accessible))
        .order_by(Document.bates_begin)
    )
    if production_id:
        query = query.where(Document.production_id == production_id)
    if tag_id:
        query = query.where(Document.tags.any(DocumentTag.tag_id == tag_id))

    result = await db.execute(query)
    docs = result.scalars().unique().all()

    # Get note counts
    doc_ids = [d.id for d in docs]
    note_counts: dict = {}
    if doc_ids:
        nc_result = await db.execute(
            select(Note.document_id, func.count(Note.id))
            .where(Note.document_id.in_(doc_ids))
            .group_by(Note.document_id)
        )
        note_counts = dict(nc_result.all())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Document ID", "Production ID", "Bates Begin", "Bates End",
        "Page Count", "Title", "Tags", "Note Count", "Has Native",
    ])

    for doc in docs:
        tags_str = "; ".join(
            f"{dt.tag.category}:{dt.tag.name}" for dt in doc.tags
        )
        writer.writerow([
            str(doc.id),
            doc.production_id,
            doc.bates_begin,
            doc.bates_end,
            doc.page_count,
            doc.title or "",
            tags_str,
            note_counts.get(doc.id, 0),
            "Yes" if doc.native_path else "No",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vigilist_export.csv"},
    )


@router.get("/search/csv")
async def export_search_csv(
    q: str = "",
    production_id: int | None = None,
    sort: str = Query("relevance", pattern="^(relevance|bates|date_asc|date_desc)$"),
    metadata: str | None = Query(None, description="JSON object of metadata key-value filters"),
    file_type: str | None = None,
    source_party: str | None = None,
    source_type: str | None = Query(None, pattern="^(collection|received)$"),
    email_address: str | None = None,
    email_scope: str = Query("from", pattern="^(from|any)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    tz: str | None = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Export search results as CSV — the same filters as /api/search, so the
    file matches the result set the reviewer is looking at."""
    from app.services import search as search_svc

    metadata_filters = None
    if metadata:
        try:
            metadata_filters = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid metadata JSON")

    accessible = await get_accessible_production_ids(db, user)
    try:
        results, total = await search_svc.search_documents(
            db, q, page=1, per_page=10000, sort=sort,
            production_id=production_id,
            accessible_production_ids=accessible,
            metadata_filters=metadata_filters,
            file_type=file_type,
            source_party=source_party,
            source_type=source_type,
            email_address=email_address,
            email_scope=email_scope,
            date_from=date_from,
            date_to=date_to,
            tz=tz,
        )
    except ValueError as e:  # unknown timezone
        raise HTTPException(status_code=422, detail=str(e))

    # The metadata report: one row per result, in result order.
    ids = [r["id"] for r in results]
    docs: dict = {}
    if ids:
        loaded = (await db.execute(
            select(Document)
            .options(selectinload(Document.tags).selectinload(DocumentTag.tag))
            .where(ids_match(Document.id, ids))
        )).scalars().unique().all()
        docs = {d.id: d for d in loaded}

    output = io.StringIO()
    output.write("﻿")  # BOM so Excel reads names and subjects as UTF-8
    writer = csv.writer(output)
    writer.writerow(["Document ID", *METADATA_HEADERS, "Snippet"])

    for r in results:
        doc = docs.get(r["id"])
        if doc is None:
            continue
        writer.writerow([
            str(doc.id),
            *metadata_row(doc, tz),
            _HIGHLIGHT.sub("", r.get("snippet") or ""),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vigilist_metadata_report.csv"},
    )


@router.get("/privilege-log/csv")
async def export_privilege_log_csv(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Export the production's privilege log as CSV."""
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    rows = await build_privilege_log_rows(db, production_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bates Begin", "Bates End", "Date", "Custodian", "Author",
                     "Recipients", "Doc Type", "Disposition", "Privilege Basis",
                     "Description", "Redaction QC"])
    for r in rows:
        writer.writerow([
            r["bates_begin"], r["bates_end"], r["doc_date"] or "",
            r["custodian"] or "", r["author"] or "", r["recipients"] or "",
            r["file_type"] or "", r["disposition"], "; ".join(r["basis"]),
            r["description"], r["qc_status"],
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=privilege_log.csv"},
    )
