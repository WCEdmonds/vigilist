import os
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Annotation, Document, DocumentTag, Note, User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids, get_user_role_for_production
from app.services.audit import log_action
from app.schemas import DocumentDetail, DocumentSummary, DocumentTagOut, PaginatedDocuments, TagOut, get_file_type

router = APIRouter(prefix="/api/documents", tags=["documents"])


FILE_TYPE_EXTENSIONS = {
    "video": [".mp4", ".mov", ".avi", ".webm"],
    "audio": [".wav", ".mp3"],
    "pdf": [".pdf"],
    "office": [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"],
    "image": [".png", ".jpg", ".jpeg", ".gif", ".bmp"],
    "email": [".msg", ".eml"],
}


@router.get("", response_model=PaginatedDocuments)
async def list_documents(
    production_id: int | None = None,
    tag_id: int | None = None,
    has_annotations: bool | None = None,
    file_type: str | None = Query(None, description="Filter by file type: video, audio, pdf, office, image, email, native, images_only"),
    sort: str = Query("bates", pattern="^(bates|recent|size)$"),
    cluster_id: int | None = None,
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

    if has_annotations is True:
        query = query.where(Document.id.in_(select(Annotation.document_id).distinct()))
        count_query = count_query.where(Document.id.in_(select(Annotation.document_id).distinct()))
    elif has_annotations is False:
        query = query.where(Document.id.notin_(select(Annotation.document_id).distinct()))
        count_query = count_query.where(Document.id.notin_(select(Annotation.document_id).distinct()))

    if file_type:
        if file_type == "native":
            query = query.where(Document.native_path.isnot(None))
            count_query = count_query.where(Document.native_path.isnot(None))
        elif file_type == "images_only":
            query = query.where(Document.native_path.is_(None))
            count_query = count_query.where(Document.native_path.is_(None))
        elif file_type in FILE_TYPE_EXTENSIONS:
            from sqlalchemy import or_
            exts = FILE_TYPE_EXTENSIONS[file_type]
            conditions = [func.lower(Document.native_path).like(f"%{ext}") for ext in exts]
            query = query.where(or_(*conditions))
            count_query = count_query.where(or_(*conditions))

    if cluster_id:
        from app.models import DocumentClusterAssignment
        query = query.where(Document.id.in_(
            select(DocumentClusterAssignment.document_id)
            .where(DocumentClusterAssignment.cluster_id == cluster_id)
        ))
        count_query = count_query.where(Document.id.in_(
            select(DocumentClusterAssignment.document_id)
            .where(DocumentClusterAssignment.cluster_id == cluster_id)
        ))

    if sort == "recent":
        # Sort by most recent activity (view, tag, note, annotation)
        from app.models import AuditLog
        latest_activity = (
            select(func.max(AuditLog.created_at))
            .where(AuditLog.resource_id == func.cast(Document.id, String))
            .correlate(Document)
            .scalar_subquery()
        )
        query = query.order_by(latest_activity.desc().nulls_last(), Document.bates_begin)
    elif sort == "size":
        query = query.order_by(Document.page_count.desc(), Document.bates_begin)
    else:
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

    ann_counts: dict = {}
    if doc_ids:
        ac_result = await db.execute(
            select(Annotation.document_id, func.count(Annotation.id))
            .where(Annotation.document_id.in_(doc_ids))
            .group_by(Annotation.document_id)
        )
        ann_counts = dict(ac_result.all())

    return PaginatedDocuments(
        documents=[
            DocumentSummary(
                id=d.id,
                production_id=d.production_id,
                bates_begin=d.bates_begin,
                bates_end=d.bates_end,
                page_count=d.page_count,
                has_native=d.native_path is not None,
                file_type=get_file_type(d.native_path, d.page_count),
                title=d.title,
                processing_status=d.processing_status,
                tags=[TagOut.model_validate(dt.tag) for dt in d.tags],
                note_count=note_counts.get(d.id, 0),
                annotation_count=ann_counts.get(d.id, 0),
            )
            for d in docs
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/random")
async def random_document(
    production_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    query = select(Document.id).where(Document.production_id.in_(accessible))
    if production_id:
        query = query.where(Document.production_id == production_id)
    query = query.order_by(func.random()).limit(1)
    result = await db.execute(query)
    doc_id = result.scalar_one_or_none()
    if not doc_id:
        raise HTTPException(status_code=404, detail="No documents found")
    return {"id": str(doc_id)}


@router.get("/metadata-keys")
async def get_metadata_keys(
    production_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return distinct metadata field keys for documents in a production."""
    accessible = await get_accessible_production_ids(db, user)
    base = select(func.jsonb_object_keys(Document.metadata_)).where(
        Document.production_id.in_(accessible)
    )
    if production_id:
        base = base.where(Document.production_id == production_id)
    result = await db.execute(base.distinct())
    keys = sorted(row[0] for row in result.all())
    return keys


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
    await log_action(db, user, "document_viewed", "document", str(doc_id), production_id=doc.production_id)
    await db.commit()
    return await _doc_detail(doc, db)


@router.put("/{doc_id}/title")
async def update_title(
    doc_id: UUID,
    body: dict,
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
    doc.title = body.get("title", "").strip() or None
    await log_action(db, user, "document_title_updated", "document", str(doc_id),
                     production_id=doc.production_id, details={"title": doc.title})
    await db.commit()
    return {"ok": True, "title": doc.title}


@router.get("/{doc_id}/image/{page_num}")
async def get_image(
    doc_id: UUID,
    page_num: int,
    w: int | None = Query(None, ge=50, le=2000, description="Resize width for thumbnails"),
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
        if w:
            import io
            from PIL import Image as PILImage
            img = PILImage.open(io.BytesIO(data))
            ratio = w / img.width
            new_h = int(img.height * ratio)
            img = img.resize((w, new_h), PILImage.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=75)
            data = buf.getvalue()
        return Response(content=data, media_type="image/jpeg")
    else:
        path = Path(raw_path.replace("\\", "/")).resolve()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Image file not found")
        return FileResponse(str(path), media_type="image/jpeg")


@router.get("/{doc_id}/pdf")
async def get_document_pdf(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate a multi-page PDF from the document's page images."""
    import io
    from PIL import Image as PILImage

    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if not doc.image_paths:
        raise HTTPException(status_code=404, detail="No page images for this document")

    def load_image(raw_path: str) -> PILImage.Image | None:
        if not raw_path:
            return None
        if raw_path.startswith("productions/"):
            from app.services.storage import get_download_bytes
            try:
                data = get_download_bytes(raw_path)
            except Exception:
                return None
            return PILImage.open(io.BytesIO(data))
        path = Path(raw_path.replace("\\", "/")).resolve()
        if not path.exists():
            return None
        return PILImage.open(str(path))

    images: list[PILImage.Image] = []
    for raw in doc.image_paths:
        img = load_image(raw)
        if img is None:
            continue
        if img.mode != "RGB":
            img = img.convert("RGB")
        images.append(img)

    if not images:
        raise HTTPException(status_code=404, detail="No readable page images")

    buf = io.BytesIO()
    first, rest = images[0], images[1:]
    first.save(buf, format="PDF", save_all=True, append_images=rest, resolution=150.0)
    pdf_bytes = buf.getvalue()

    filename = f"{doc.bates_begin}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@router.get("/{doc_id}/native-url")
async def get_native_url(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a signed URL for the native file (for browser-based viewing)."""
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
        url = get_signed_url(doc.native_path, expiration_minutes=60)
        suffix = doc.native_path.rsplit(".", 1)[-1].lower() if "." in doc.native_path else ""
        return {"url": url, "extension": suffix, "filename": doc.native_path.rsplit("/", 1)[-1]}

    # Local dev: return the regular endpoint URL (auth handled by cookie/session)
    return {"url": f"/api/documents/{doc_id}/native", "extension": doc.native_path.rsplit(".", 1)[-1].lower(), "filename": doc.native_path.rsplit("/", 1)[-1]}


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

    annotation_count = (await db.execute(
        select(func.count(Annotation.id)).where(Annotation.document_id == doc.id)
    )).scalar() or 0

    return DocumentDetail(
        id=doc.id,
        production_id=doc.production_id,
        bates_begin=doc.bates_begin,
        bates_end=doc.bates_end,
        page_count=doc.page_count,
        title=doc.title,
        summary=doc.summary,
        processing_status=doc.processing_status,
        metadata=doc.metadata_ or {},
        text_content=doc.text_content,
        native_path=doc.native_path,
        image_paths=doc.image_paths or [],
        tags=[DocumentTagOut(id=dt.id, tag=TagOut.model_validate(dt.tag), applied_by=dt.applied_by, applied_at=dt.applied_at) for dt in doc.tags],
        note_count=note_count,
        annotation_count=annotation_count,
    )
