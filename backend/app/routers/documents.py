import os
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Document, DocumentTag, Note, User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids
from app.schemas import DocumentDetail, DocumentSummary, DocumentTagOut, PaginatedDocuments, TagOut

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("", response_model=PaginatedDocuments)
async def list_documents(
    production_id: int | None = None,
    tag_id: int | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    query = select(Document).options(
        selectinload(Document.tags).selectinload(DocumentTag.tag)
    ).where(Document.production_id.in_(accessible))
    count_query = select(func.count(Document.id)).where(Document.production_id.in_(accessible))

    if production_id:
        query = query.where(Document.production_id == production_id)
        count_query = count_query.where(Document.production_id == production_id)

    if tag_id:
        query = query.where(Document.tags.any(DocumentTag.tag_id == tag_id))
        count_query = count_query.where(Document.id.in_(
            select(DocumentTag.document_id).where(DocumentTag.tag_id == tag_id)
        ))

    query = query.order_by(Document.bates_begin)
    total = (await db.execute(count_query)).scalar() or 0

    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    docs = result.scalars().unique().all()

    # Get note counts for these docs
    doc_ids = [d.id for d in docs]
    note_counts: dict = {}
    if doc_ids:
        nc_result = await db.execute(
            select(Note.document_id, func.count(Note.id))
            .where(Note.document_id.in_(doc_ids))
            .group_by(Note.document_id)
        )
        note_counts = dict(nc_result.all())

    return PaginatedDocuments(
        documents=[
            DocumentSummary(
                id=d.id,
                production_id=d.production_id,
                bates_begin=d.bates_begin,
                bates_end=d.bates_end,
                page_count=d.page_count,
                has_native=d.native_path is not None,
                title=d.title,
                tags=[TagOut.model_validate(dt.tag) for dt in d.tags],
                note_count=note_counts.get(d.id, 0),
            )
            for d in docs
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/by-bates")
async def get_by_bates(
    bates: str,
    production_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    query = (
        select(Document)
        .where(Document.bates_begin == bates)
        .options(selectinload(Document.tags).selectinload(DocumentTag.tag))
    )
    if production_id:
        query = query.where(Document.production_id == production_id)
    result = await db.execute(query)
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    return await _doc_detail(doc, db)


@router.get("/{doc_id}", response_model=DocumentDetail)
async def get_document(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    result = await db.execute(
        select(Document)
        .where(Document.id == doc_id)
        .options(selectinload(Document.tags).selectinload(DocumentTag.tag))
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    return await _doc_detail(doc, db)


@router.get("/{doc_id}/image/{page_num}")
async def get_image(
    doc_id: UUID,
    page_num: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if page_num < 1 or page_num > len(doc.image_paths):
        raise HTTPException(status_code=404, detail="Page not found")
    raw_path = doc.image_paths[page_num - 1]
    if not raw_path:
        raise HTTPException(status_code=404, detail="Image file not found")

    if raw_path.startswith("productions/"):
        from app.services.storage import get_download_bytes
        try:
            data = get_download_bytes(raw_path)
        except Exception:
            raise HTTPException(status_code=404, detail="Image file not found in storage")
        return Response(content=data, media_type="image/jpeg")
    else:
        path = Path(raw_path.replace("\\", "/")).resolve()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Image file not found")
        return FileResponse(str(path), media_type="image/jpeg")


@router.get("/{doc_id}/native")
async def get_native(
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
    if not doc.native_path:
        raise HTTPException(status_code=404, detail="No native file for this document")

    if doc.native_path.startswith("productions/"):
        from app.services.storage import get_download_bytes
        suffix = doc.native_path.rsplit(".", 1)[-1].lower() if "." in doc.native_path else ""
        media_types = {
            "pdf": "application/pdf", "mp4": "video/mp4", "mov": "video/quicktime",
            "wav": "audio/wav", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "txt": "text/plain",
        }
        media_type = media_types.get(suffix, "application/octet-stream")
        filename = doc.native_path.rsplit("/", 1)[-1]
        try:
            data = get_download_bytes(doc.native_path)
        except Exception:
            raise HTTPException(status_code=404, detail="Native file not found in storage")
        return Response(
            content=data,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    else:
        path = Path(doc.native_path.replace("\\", "/")).resolve()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Native file not found on disk")
        suffix = path.suffix.lower()
        media_types = {
            ".pdf": "application/pdf",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".msg": "application/vnd.ms-outlook",
            ".eml": "message/rfc822",
            ".txt": "text/plain",
            ".csv": "text/csv",
            ".html": "text/html",
            ".htm": "text/html",
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".wav": "audio/wav",
        }
        media_type = media_types.get(suffix, "application/octet-stream")
        return FileResponse(str(path), media_type=media_type, filename=path.name)


STREAM_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".wav": "audio/wav",
}

STREAM_CHUNK_SIZE = 1024 * 1024  # 1MB


@router.get("/{doc_id}/stream")
async def stream_native(
    doc_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if not doc.native_path:
        raise HTTPException(status_code=404, detail="No native file for this document")

    if doc.native_path.startswith("productions/"):
        from app.services.storage import get_signed_url
        from fastapi.responses import RedirectResponse
        suffix = doc.native_path.rsplit(".", 1)[-1].lower() if "." in doc.native_path else ""
        media_type = STREAM_MEDIA_TYPES.get(f".{suffix}")
        if not media_type:
            raise HTTPException(status_code=400, detail="File is not a streamable media type")
        url = get_signed_url(doc.native_path, expiration_minutes=60)
        return RedirectResponse(url=url)

    path = Path(doc.native_path.replace("\\", "/")).resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Native file not found on disk")

    suffix = path.suffix.lower()
    media_type = STREAM_MEDIA_TYPES.get(suffix)
    if not media_type:
        raise HTTPException(status_code=400, detail="File is not a streamable media type")

    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        # Parse "bytes=start-end"
        range_spec = range_header.strip().replace("bytes=", "")
        parts = range_spec.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else min(start + STREAM_CHUNK_SIZE - 1, file_size - 1)
        end = min(end, file_size - 1)

        content_length = end - start + 1

        def iter_range():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(STREAM_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iter_range(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
            },
        )

    # No range header — return full file
    def iter_file():
        with open(path, "rb") as f:
            while chunk := f.read(STREAM_CHUNK_SIZE):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
    )


@router.get("/{doc_id}/text")
async def get_text(
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
    return {"text": doc.text_content or ""}


@router.get("/{doc_id}/nav")
async def get_nav(
    doc_id: UUID,
    production_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    prod_filter = Document.production_id == (production_id or doc.production_id)

    prev_q = (
        select(Document.id)
        .where(prod_filter, Document.bates_begin < doc.bates_begin)
        .order_by(Document.bates_begin.desc())
        .limit(1)
    )
    next_q = (
        select(Document.id)
        .where(prod_filter, Document.bates_begin > doc.bates_begin)
        .order_by(Document.bates_begin)
        .limit(1)
    )

    prev_id = (await db.execute(prev_q)).scalar_one_or_none()
    next_id = (await db.execute(next_q)).scalar_one_or_none()

    return {
        "prev_id": str(prev_id) if prev_id else None,
        "next_id": str(next_id) if next_id else None,
    }


async def _doc_detail(doc: Document, db: AsyncSession) -> DocumentDetail:
    note_count = (await db.execute(
        select(func.count(Note.id)).where(Note.document_id == doc.id)
    )).scalar() or 0

    return DocumentDetail(
        id=doc.id,
        production_id=doc.production_id,
        bates_begin=doc.bates_begin,
        bates_end=doc.bates_end,
        page_count=doc.page_count,
        title=doc.title,
        summary=doc.summary,
        metadata=doc.metadata_ or {},
        text_content=doc.text_content,
        native_path=doc.native_path,
        image_paths=doc.image_paths or [],
        tags=[DocumentTagOut(id=dt.id, tag=TagOut.model_validate(dt.tag), applied_by=dt.applied_by, applied_at=dt.applied_at) for dt in doc.tags],
        note_count=note_count,
    )
