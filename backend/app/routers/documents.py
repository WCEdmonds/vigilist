import os
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Annotation, Document, DocumentTag, Note, Redaction, User
from app.models_review import ReviewProject, AIReviewResult

# Pin colors used by both the viewer overlay and the burned-in PDF pins.
PDF_PIN_COLORS: dict[str, tuple[int, int, int]] = {
    "red": (229, 62, 62),
    "yellow": (236, 201, 75),
    "green": (72, 187, 120),
    "blue": (66, 153, 225),
}
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids, get_user_role_for_production
from app.services.audit import log_action
from app.schemas import DocumentDetail, DocumentSummary, DocumentTagOut, PaginatedDocuments, TagOut, get_file_type
from app.services.redaction_render import burn_page

router = APIRouter(prefix="/api/documents", tags=["documents"])


FILE_TYPE_EXTENSIONS = {
    "video": [".mp4", ".mov", ".avi", ".webm"],
    "audio": [".wav", ".mp3"],
    "pdf": [".pdf"],
    "office": [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"],
    "image": [".png", ".jpg", ".jpeg", ".gif", ".bmp"],
    "email": [".msg", ".eml"],
}


def cluster_label_map(rows) -> dict[str, dict]:
    """(document_id, cluster_id, label) tuples -> {doc_id_str: {cluster_id, cluster_label}}."""
    return {
        str(doc_id): {"cluster_id": cluster_id, "cluster_label": label}
        for doc_id, cluster_id, label in rows
    }


def ai_decision_map(rows) -> dict[str, dict]:
    """(document_id, ai_decision, confidence_score, attorney_decision) tuples -> {doc_id_str: {ai_decision, ai_confidence, ai_decided}}."""
    return {
        str(doc_id): {"ai_decision": ai_decision, "ai_confidence": confidence, "ai_decided": bool(attorney_decision)}
        for doc_id, ai_decision, confidence, attorney_decision in rows
    }


@router.get("", response_model=PaginatedDocuments)
async def list_documents(
    production_id: int | None = None,
    tag_id: int | None = None,
    has_annotations: bool | None = None,
    file_type: str | None = Query(None, description="Filter by file type: video, audio, pdf, office, image, email, native, images_only"),
    source_party: str | None = Query(None, description="Filter by source party label"),
    source_type: str | None = Query(None, pattern="^(collection|received)$"),
    sort: str = Query("bates", pattern="^(bates|recent|size)$"),
    cluster_id: int | None = None,
    ai_decision: str | None = None,
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

    if source_party:
        query = query.where(Document.source_party == source_party)
        count_query = count_query.where(Document.source_party == source_party)
    if source_type == "received":
        query = query.where(Document.source_type == "received")
        count_query = count_query.where(Document.source_type == "received")
    elif source_type == "collection":
        # NULL counts as ours — see services/search.py rationale.
        query = query.where(Document.source_type.is_distinct_from("received"))
        count_query = count_query.where(Document.source_type.is_distinct_from("received"))

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

    # Get primary project for ai_decision filtering
    primary_project_id = None
    if production_id and ai_decision:
        primary_project_result = await db.execute(
            select(ReviewProject.id).where(
                ReviewProject.production_id == production_id,
                ReviewProject.is_primary == True
            )
        )
        primary_project_id = primary_project_result.scalar_one_or_none()

    if ai_decision:
        if primary_project_id:
            # Filter by ai_decision via EXISTS subquery
            query = query.where(Document.id.in_(
                select(AIReviewResult.document_id)
                .where(
                    AIReviewResult.project_id == primary_project_id,
                    AIReviewResult.ai_decision == ai_decision
                )
            ))
            count_query = count_query.where(Document.id.in_(
                select(AIReviewResult.document_id)
                .where(
                    AIReviewResult.project_id == primary_project_id,
                    AIReviewResult.ai_decision == ai_decision
                )
            ))
        else:
            # No primary project exists, return empty page
            query = query.where(False)
            count_query = count_query.where(False)

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

    # Get cluster assignments for these docs
    from app.models import DocumentClusterAssignment, DocumentCluster
    cluster_rows = []
    if doc_ids:
        cluster_rows = (
            await db.execute(
                select(
                    DocumentClusterAssignment.document_id,
                    DocumentClusterAssignment.cluster_id,
                    DocumentCluster.label,
                )
                .join(DocumentCluster, DocumentCluster.id == DocumentClusterAssignment.cluster_id)
                .where(DocumentClusterAssignment.document_id.in_(doc_ids))
            )
        ).all()
    clusters_by_doc = cluster_label_map(cluster_rows)

    # Get AI decision enrichment for these docs
    # Get primary project if not already retrieved (only happens when ai_decision filter is NOT set)
    if production_id and primary_project_id is None and not ai_decision:
        primary_project_result = await db.execute(
            select(ReviewProject.id).where(
                ReviewProject.production_id == production_id,
                ReviewProject.is_primary == True
            )
        )
        primary_project_id = primary_project_result.scalar_one_or_none()

    ai_decisions_by_doc = {}
    if doc_ids and primary_project_id:
        ai_rows = (
            await db.execute(
                select(
                    AIReviewResult.document_id,
                    AIReviewResult.ai_decision,
                    AIReviewResult.confidence_score,
                    AIReviewResult.attorney_decision,
                )
                .where(
                    AIReviewResult.project_id == primary_project_id,
                    AIReviewResult.document_id.in_(doc_ids)
                )
            )
        ).all()
        ai_decisions_by_doc = ai_decision_map(ai_rows)

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
                cluster_id=(clusters_by_doc.get(str(d.id)) or {}).get("cluster_id"),
                cluster_label=(clusters_by_doc.get(str(d.id)) or {}).get("cluster_label"),
                ai_decision=(ai_decisions_by_doc.get(str(d.id)) or {}).get("ai_decision"),
                ai_confidence=(ai_decisions_by_doc.get(str(d.id)) or {}).get("ai_confidence"),
                ai_decided=(ai_decisions_by_doc.get(str(d.id)) or {}).get("ai_decided", False),
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


@router.get("/source-parties")
async def list_source_parties(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Distinct source-party labels for the source filter dropdown (P0-SP5)."""
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    rows = (await db.execute(
        select(Document.source_party)
        .where(Document.production_id == production_id,
               Document.source_party.is_not(None))
        .distinct()
        .order_by(Document.source_party)
    )).all()
    return {"source_parties": [r[0] for r in rows]}


@router.get("/by-bates")
async def get_by_bates(
    bates: str,
    production_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)

    def _scoped(q):
        # Scope up front (rather than post-checking) so the same Bates
        # existing in two accessible productions can't blow up
        # scalar_one_or_none, and out-of-scope docs are never even matched.
        if production_id:
            return q.where(Document.production_id == production_id)
        return q.where(Document.production_id.in_(accessible))

    base = select(Document).options(
        selectinload(Document.tags).selectinload(DocumentTag.tag)
    )
    result = await db.execute(
        _scoped(base.where(Document.bates_begin == bates)).limit(1)
    )
    doc = result.scalars().first()

    if not doc:
        # AI-written references (brief, chat) often reformat Bates separators
        # ("SCHLEGEL 000068" vs "SCHLEGEL-000068") — fall back to comparing
        # with all non-alphanumerics stripped, case-insensitively.
        normalized = "".join(c for c in bates if c.isalnum()).upper()
        if normalized:
            result = await db.execute(
                _scoped(
                    base.where(
                        func.upper(
                            func.regexp_replace(Document.bates_begin, "[^A-Za-z0-9]", "", "g")
                        )
                        == normalized
                    )
                ).limit(1)
            )
            doc = result.scalars().first()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
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
    redacted: bool = Query(False, description="Burn redactions into the returned image"),
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

    rects = []
    if redacted:
        result = await db.execute(
            select(Redaction).where(
                Redaction.document_id == doc_id, Redaction.page_num == page_num
            )
        )
        rects = list(result.scalars().all())

    if raw_path.startswith("productions/"):
        from app.services.storage import get_download_bytes
        try:
            data = get_download_bytes(raw_path)
        except Exception:
            raise HTTPException(status_code=404, detail="Image file not found in storage")
        if w or rects:
            import io
            from PIL import Image as PILImage, UnidentifiedImageError
            try:
                img = PILImage.open(io.BytesIO(data))
                img.load()
            except (UnidentifiedImageError, OSError):
                raise HTTPException(status_code=404, detail="Image file unreadable")
            if rects:
                img = burn_page(img, rects)
            if w:
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
        if rects:
            import io
            from PIL import Image as PILImage, UnidentifiedImageError
            try:
                img = PILImage.open(str(path))
                img.load()
            except (UnidentifiedImageError, OSError):
                raise HTTPException(status_code=404, detail="Image file unreadable")
            img = burn_page(img, rects)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            return Response(content=buf.getvalue(), media_type="image/jpeg")
        return FileResponse(str(path), media_type="image/jpeg")


@router.get("/{doc_id}/pdf")
async def get_document_pdf(
    doc_id: UUID,
    redacted: bool = Query(False, description="As-produced rendition: burn redactions, omit annotations"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate a multi-page PDF from the document's page images.

    Default: annotations burned in as numbered pins plus an annotation index
    appended at the end. With redacted=1: the as-produced view — redaction
    boxes burned in, no pins, no index.
    """
    import io
    import logging
    from PIL import Image as PILImage, ImageDraw, ImageFont, UnidentifiedImageError

    logger = logging.getLogger(__name__)

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
        try:
            if raw_path.startswith("productions/"):
                from app.services.storage import get_download_bytes
                data = get_download_bytes(raw_path)
                return PILImage.open(io.BytesIO(data))
            path = Path(raw_path.replace("\\", "/")).resolve()
            if not path.exists():
                logger.warning("PDF build: local image missing: %s", raw_path)
                return None
            return PILImage.open(str(path))
        except (UnidentifiedImageError, OSError) as e:
            logger.warning("PDF build: could not open image %s: %s", raw_path, e)
            return None
        except Exception as e:
            logger.exception("PDF build: unexpected error loading %s: %s", raw_path, e)
            return None

    # Load pages, tracking the original 1-based page number so annotation
    # page_num keeps pointing at the right image even if some pages fail.
    loaded: list[tuple[int, PILImage.Image]] = []
    load_errors = 0
    for idx, raw in enumerate(doc.image_paths, start=1):
        img = load_image(raw)
        if img is None:
            load_errors += 1
            continue
        try:
            if img.mode != "RGB":
                img = img.convert("RGB")
            loaded.append((idx, img))
        except Exception as e:
            logger.exception("PDF build: failed to convert image %s: %s", raw, e)
            load_errors += 1

    if not loaded:
        logger.error(
            "PDF build: no readable pages for doc %s (paths=%d, failures=%d)",
            doc.id, len(doc.image_paths), load_errors,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not build PDF: none of the page images could be read.",
        )

    annotations: list[Annotation] = []
    if redacted:
        # As-produced: burn redaction boxes; annotations are work product
        # and are omitted entirely (no pins, no index pages).
        red_result = await db.execute(
            select(Redaction).where(Redaction.document_id == doc.id)
        )
        red_by_page: dict[int, list[Redaction]] = {}
        for r in red_result.scalars().all():
            red_by_page.setdefault(r.page_num, []).append(r)
        loaded = [
            (idx, burn_page(img, red_by_page[idx]) if idx in red_by_page else img)
            for idx, img in loaded
        ]
    else:
        ann_result = await db.execute(
            select(Annotation)
            .where(Annotation.document_id == doc.id)
            .order_by(Annotation.page_num, Annotation.created_at)
        )
        annotations = list(ann_result.scalars().all())

    by_page: dict[int, list[Annotation]] = {}
    for a in annotations:
        by_page.setdefault(a.page_num, []).append(a)

    author_names: dict[str, str] = {}
    if annotations:
        unique_ids = {a.created_by for a in annotations}
        for uid in unique_ids:
            u = await db.get(User, uid)
            if u:
                author_names[uid] = u.display_name or u.email or uid
            else:
                author_names[uid] = uid

    # Fonts — DejaVu is installed in the Dockerfile; fall back to PIL's
    # built-in bitmap font if it's missing (dev without the TTF installed).
    def _load_font(path: str, size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()

    DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Burn pins into each page image, sized relative to the page so they
    # stay readable across both small and large-format scans.
    try:
        for page_num, img in loaded:
            page_anns = by_page.get(page_num)
            if not page_anns:
                continue
            draw = ImageDraw.Draw(img)
            pin_radius = max(14, min(img.width, img.height) // 60)
            pin_font = _load_font(DEJAVU_BOLD, max(12, pin_radius))
            for i, a in enumerate(page_anns, start=1):
                cx = a.x_pct / 100.0 * img.width
                cy = a.y_pct / 100.0 * img.height
                color = PDF_PIN_COLORS.get(a.color, PDF_PIN_COLORS["blue"])
                draw.ellipse(
                    (cx - pin_radius, cy - pin_radius, cx + pin_radius, cy + pin_radius),
                    fill=color,
                    outline="white",
                    width=max(2, pin_radius // 6),
                )
                draw.text((cx, cy), str(i), fill="white", anchor="mm", font=pin_font)
    except Exception as e:
        logger.exception("PDF build: failed to draw annotation pins: %s", e)
        # Keep going — we'd rather ship a plain PDF than fail the download.

    # Build the annotation index pages (appended after the document).
    def _wrap(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [""]:
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            cur = ""
            for w in words:
                trial = f"{cur} {w}".strip()
                bbox = draw.textbbox((0, 0), trial, font=font)
                if bbox[2] - bbox[0] <= max_width:
                    cur = trial
                else:
                    if cur:
                        lines.append(cur)
                    cur = w
            if cur:
                lines.append(cur)
        return lines

    def _build_index_pages() -> list[PILImage.Image]:
        if not annotations:
            return []
        page_w, page_h = 1240, 1754  # A4 @ ~150 DPI
        margin = 80
        title_font = _load_font(DEJAVU_BOLD, 36)
        head_font = _load_font(DEJAVU_BOLD, 22)
        body_font = _load_font(DEJAVU, 16)
        meta_font = _load_font(DEJAVU, 13)
        line_h = 22
        meta_line_h = 18
        pin_r = 14

        pages: list[PILImage.Image] = []

        def new_page(first: bool) -> tuple[PILImage.Image, ImageDraw.ImageDraw, int]:
            img = PILImage.new("RGB", (page_w, page_h), "white")
            d = ImageDraw.Draw(img)
            y_pos = margin
            if first:
                d.text((margin, y_pos), "Annotations", fill="black", font=title_font)
                y_pos += 52
                d.text(
                    (margin, y_pos),
                    f"{doc.bates_begin} · {len(annotations)} annotation"
                    f"{'' if len(annotations) == 1 else 's'}",
                    fill=(100, 100, 100),
                    font=meta_font,
                )
                y_pos += 28
                d.line([(margin, y_pos), (page_w - margin, y_pos)], fill=(200, 200, 200), width=1)
                y_pos += 20
            return img, d, y_pos

        img, draw, y = new_page(first=True)

        def ensure_space(needed: int) -> None:
            nonlocal img, draw, y
            if y + needed > page_h - margin:
                pages.append(img)
                img, draw, y = new_page(first=False)

        text_indent = margin + 40  # leaves room for the pin indicator
        text_width = page_w - text_indent - margin

        for page_num in sorted(by_page.keys()):
            ensure_space(40)
            draw.text((margin, y), f"Page {page_num}", fill="black", font=head_font)
            y += 34

            for i, a in enumerate(by_page[page_num], start=1):
                content = (a.content or "").strip() or "(no content)"
                wrapped = _wrap(content, body_font, text_width, draw)
                block_height = max(pin_r * 2 + 4, len(wrapped) * line_h + meta_line_h + 10)
                ensure_space(block_height)

                # Pin indicator with matching number
                pin_cx = margin + pin_r
                pin_cy = y + pin_r
                color = PDF_PIN_COLORS.get(a.color, PDF_PIN_COLORS["blue"])
                draw.ellipse(
                    (pin_cx - pin_r, pin_cy - pin_r, pin_cx + pin_r, pin_cy + pin_r),
                    fill=color,
                    outline=(60, 60, 60),
                    width=1,
                )
                draw.text((pin_cx, pin_cy), str(i), fill="white", anchor="mm", font=meta_font)

                line_y = y
                for line in wrapped:
                    draw.text((text_indent, line_y), line, fill="black", font=body_font)
                    line_y += line_h

                author = author_names.get(a.created_by, a.created_by)
                date_str = a.created_at.strftime("%Y-%m-%d %H:%M") if a.created_at else ""
                meta = f"— {author}" + (f", {date_str}" if date_str else "")
                draw.text((text_indent, line_y + 2), meta, fill=(110, 110, 110), font=meta_font)
                y = line_y + meta_line_h + 14

            y += 12  # spacing between page groups

        pages.append(img)
        return pages

    index_pages: list[PILImage.Image] = []
    try:
        index_pages = _build_index_pages()
    except Exception as e:
        logger.exception("PDF build: failed to build annotation index pages: %s", e)

    all_pages: list[PILImage.Image] = [img for _, img in loaded] + index_pages

    try:
        buf = io.BytesIO()
        first, rest = all_pages[0], all_pages[1:]
        first.save(buf, format="PDF", save_all=True, append_images=rest, resolution=150.0)
        pdf_bytes = buf.getvalue()
    except Exception as e:
        logger.exception("PDF build: Pillow save failed for doc %s: %s", doc.id, e)
        raise HTTPException(
            status_code=500,
            detail=f"PDF generation failed: {e}",
        )

    filename = f"{doc.bates_begin}_redacted.pdf" if redacted else f"{doc.bates_begin}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class BulkZipRequest(BaseModel):
    document_ids: list[UUID]


@router.post("/bulk-zip")
async def bulk_zip_documents(
    body: BulkZipRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Package multiple documents into a single ZIP file for download.
    Native files are included as-is; documents without a native fall back
    to a PDF render of their page images."""
    import io
    import logging
    import zipfile
    from PIL import Image as PILImage, UnidentifiedImageError

    logger = logging.getLogger(__name__)

    if not body.document_ids:
        raise HTTPException(status_code=400, detail="No documents selected")
    if len(body.document_ids) > 500:
        raise HTTPException(status_code=400, detail="Too many documents (max 500 per bulk download)")

    accessible = await get_accessible_production_ids(db, user)

    # Load all requested docs in one query and filter by access.
    result = await db.execute(select(Document).where(Document.id.in_(body.document_ids)))
    docs = [d for d in result.scalars().all() if d.production_id in accessible]

    if not docs:
        raise HTTPException(status_code=404, detail="None of the requested documents were found or accessible")

    from app.services.storage import get_download_bytes

    # Ensure unique filenames within the zip (two docs can share a bates prefix).
    used_names: set[str] = set()

    def unique_name(base: str) -> str:
        name = base
        i = 1
        while name in used_names:
            stem, _, ext = base.rpartition(".")
            name = f"{stem}_{i}.{ext}" if stem else f"{base}_{i}"
            i += 1
        used_names.add(name)
        return name

    def render_pdf_from_images(doc: Document) -> bytes | None:
        if not doc.image_paths:
            return None
        imgs: list[PILImage.Image] = []
        for raw in doc.image_paths:
            if not raw:
                continue
            try:
                if raw.startswith("productions/"):
                    data = get_download_bytes(raw)
                    img = PILImage.open(io.BytesIO(data))
                else:
                    p = Path(raw.replace("\\", "/")).resolve()
                    if not p.exists():
                        continue
                    img = PILImage.open(str(p))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                imgs.append(img)
            except (UnidentifiedImageError, OSError) as e:
                logger.warning("bulk-zip: could not open image %s: %s", raw, e)
                continue
        if not imgs:
            return None
        try:
            buf = io.BytesIO()
            first, rest = imgs[0], imgs[1:]
            first.save(buf, format="PDF", save_all=True, append_images=rest, resolution=150.0)
            return buf.getvalue()
        except Exception as e:
            logger.exception("bulk-zip: PDF render failed for doc %s: %s", doc.id, e)
            return None

    zip_buf = io.BytesIO()
    skipped: list[str] = []
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            try:
                if doc.native_path:
                    # Native file as-is.
                    try:
                        if doc.native_path.startswith("productions/"):
                            data = get_download_bytes(doc.native_path)
                        else:
                            p = Path(doc.native_path.replace("\\", "/")).resolve()
                            if not p.exists():
                                skipped.append(doc.bates_begin)
                                continue
                            data = p.read_bytes()
                        ext = Path(doc.native_path).suffix or ".bin"
                        name = unique_name(f"{doc.bates_begin}{ext}")
                        zf.writestr(name, data)
                        continue
                    except Exception as e:
                        logger.warning("bulk-zip: native fetch failed for %s: %s", doc.bates_begin, e)
                        # Fall through to PDF render below.

                pdf = render_pdf_from_images(doc)
                if pdf:
                    name = unique_name(f"{doc.bates_begin}.pdf")
                    zf.writestr(name, pdf)
                else:
                    skipped.append(doc.bates_begin)
            except Exception as e:
                logger.exception("bulk-zip: unexpected failure for %s: %s", doc.bates_begin, e)
                skipped.append(doc.bates_begin)

        if skipped:
            zf.writestr(
                "SKIPPED.txt",
                "The following documents could not be included:\n\n"
                + "\n".join(skipped),
            )

    await log_action(
        db, user, "bulk_download", "document",
        details={"requested": len(body.document_ids), "included": len(docs) - len(skipped), "skipped": len(skipped)},
    )
    await db.commit()

    zip_bytes = zip_buf.getvalue()
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vigilist_documents.zip"'},
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
    download: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a signed URL for the native file (for browser-based viewing or download).

    Pass ?download=true to get a URL whose response will carry
    Content-Disposition: attachment, forcing a save-file dialog instead of
    inline playback (needed for video/audio on cross-origin signed URLs).
    """
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
        filename = doc.native_path.rsplit("/", 1)[-1]
        suffix = doc.native_path.rsplit(".", 1)[-1].lower() if "." in doc.native_path else ""
        response_disposition = f'attachment; filename="{filename}"' if download else None
        url = get_signed_url(doc.native_path, expiration_minutes=60, response_disposition=response_disposition)
        return {"url": url, "extension": suffix, "filename": filename}

    # Local dev: return the regular endpoint URL (auth handled by cookie/session)
    # The /native endpoint already sends Content-Disposition: attachment.
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
    redacted: bool = Query(False, description="Withhold extracted text if the document has redactions"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if redacted:
        # Flat text_content has no word coordinates, so region-level removal
        # is impossible — the as-produced text for a redacted doc is withheld
        # entirely (re-OCR of burned images is Phase 2).
        count = (await db.execute(
            select(func.count(Redaction.id)).where(Redaction.document_id == doc_id)
        )).scalar() or 0
        if count:
            return {"text": "", "withheld": True}
        return {"text": doc.text_content or "", "withheld": False}
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

    redaction_count = (await db.execute(
        select(func.count(Redaction.id)).where(Redaction.document_id == doc.id)
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
        redaction_count=redaction_count,
    )
