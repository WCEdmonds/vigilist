"""Export endpoints: CSV export of document metadata, tags, and notes."""

import csv
import io
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Document, DocumentTag, Note, User
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/documents/csv")
async def export_documents_csv(
    production_id: int | None = None,
    tag_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Export documents as CSV with metadata, tags, and note counts."""
    query = (
        select(Document)
        .options(selectinload(Document.tags).selectinload(DocumentTag.tag))
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
        headers={"Content-Disposition": "attachment; filename=descubre_export.csv"},
    )


@router.get("/search/csv")
async def export_search_csv(
    q: str,
    production_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Export search results as CSV."""
    from app.services.search import search_documents

    results, total = await search_documents(
        db, q, page=1, per_page=10000,
        production_id=production_id,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Document ID", "Bates Begin", "Bates End", "Page Count",
        "Title", "Snippet", "Rank",
    ])

    for r in results:
        writer.writerow([
            str(r["id"]),
            r["bates_begin"],
            r["bates_end"],
            r["page_count"],
            r.get("title", ""),
            r.get("snippet", ""),
            r.get("rank", ""),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=descubre_search_export.csv"},
    )
