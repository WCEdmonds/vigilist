"""Image documents on demand so they can be redacted (and produced as images).

Emails and email attachments are ingested without page images. This turns an
email (laid out by email_pages), a PDF native, or an image native into page
JPEGs plus word sidecars, stored exactly like an ingested PDF's pages
(ingest_pdf.upload_page_assets), so redaction, burn-in and produced text work
the same way for them.

Pages are frozen once a document has a redaction: re-imaging could move the
content under existing boxes, so it is refused.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update

from app.models import Document, Redaction
from app.services.email_pages import render_email_pages
from app.services.ingest_pdf import iter_pdf_pages, upload_page_assets
from app.services.storage import get_download_bytes

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
# A queued/running job older than this is dead and may be claimed again
# (Cloud Tasks' dispatch deadline is 30 minutes). Running jobs are timed
# from when a worker claimed them, not from when they were queued.
STALE_IMAGING = timedelta(minutes=35)
Kind = Literal["email", "pdf", "image"]


class ImagingRefused(Exception):
    """The document can't be (re-)imaged, e.g. it already has redactions."""


def imaging_kind(doc) -> Kind | None:
    """How this document would be imaged, or None if it can't (or needn't) be."""
    if doc.image_paths:
        return None
    if doc.file_type == "email":
        return "email"
    ext = os.path.splitext(doc.native_path or "")[1].lower()
    if not doc.native_path:
        return None
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def build_pages(doc, kind: Kind, attachment_names: list[str], *, ocr_fn
                ) -> tuple[list[str], list[str], int, dict | None]:
    """Render and upload the document's pages.
    Returns (image_paths, ocr_paths, page_count, header_regions)."""
    image_paths: list[str] = []
    ocr_paths: list[str] = []
    errors: list[str] = []
    regions = None

    if kind == "image":
        from PIL import Image, ImageSequence

        img = Image.open(io.BytesIO(get_download_bytes(doc.native_path)))
        for n, frame in enumerate(ImageSequence.Iterator(img), start=1):
            buf = io.BytesIO()
            frame.convert("RGB").save(buf, format="JPEG", quality=90)
            jpeg = buf.getvalue()
            upload_page_assets(doc.production_id, doc.bates_begin, n, jpeg, ocr_fn(jpeg),
                               image_paths, ocr_paths, errors)
    else:
        if kind == "email":
            pages = render_email_pages(doc, attachment_names)
            pdf, regions = pages.pdf, pages.header_regions
        else:
            pdf = get_download_bytes(doc.native_path)
        for n, jpeg, page_ocr in iter_pdf_pages(pdf, ocr_fn):
            upload_page_assets(doc.production_id, doc.bates_begin, n, jpeg, page_ocr,
                               image_paths, ocr_paths, errors)

    if errors or not image_paths or not all(image_paths):
        raise RuntimeError("; ".join(errors) or "no pages produced")
    return image_paths, ocr_paths, len(image_paths), regions


async def image_document(db, doc_id: UUID) -> None:
    """Worker entry point. Records failure on the row; raises ImagingRefused
    only for documents that must not be imaged."""
    from app.services.ingest_pdf import _ocr_jpeg

    doc = await db.get(Document, doc_id)
    if doc is None:
        return
    if doc.image_paths:
        # Duplicate or retried task for a document that already has pages.
        if doc.imaging_status in ("queued", "running"):
            doc.imaging_status = "done"
            await db.commit()
        return
    kind = imaging_kind(doc)
    redactions = (await db.execute(
        select(func.count(Redaction.id)).where(Redaction.document_id == doc_id)
    )).scalar() or 0
    if redactions or kind is None:
        doc.imaging_status = "failed"
        doc.imaging_error = ("has redactions; pages are frozen" if redactions
                             else "this file type can't be imaged")
        await db.commit()
        raise ImagingRefused(doc.imaging_error)

    names: list[str] = []
    if kind == "email" and doc.family_id:
        names = [n for (n,) in (await db.execute(
            select(Document.file_name)
            .where(Document.production_id == doc.production_id,
                   Document.family_id == doc.family_id, Document.id != doc.id)
            .order_by(Document.bates_begin)
        )).all() if n]

    # Claim the job: only one worker may move it to running.
    now = datetime.now(timezone.utc)
    claimed = (await db.execute(
        update(Document)
        .where(Document.id == doc_id, or_(
            Document.imaging_status.is_(None),
            Document.imaging_status.in_(("queued", "failed")),
            and_(Document.imaging_status == "running",
                 Document.imaging_requested_at < now - STALE_IMAGING),
        ))
        .values(imaging_status="running", imaging_error=None, imaging_requested_at=now)
        .execution_options(synchronize_session=False)
    )).rowcount
    if not claimed:
        return
    doc.imaging_status, doc.imaging_error, doc.imaging_requested_at = "running", None, now
    # Release the connection before minutes of rendering/OCR: Neon kills
    # connections idling inside a transaction (PR #87/#88).
    await db.commit()
    try:
        image_paths, ocr_paths, count, regions = await asyncio.to_thread(
            build_pages, doc, kind, names, ocr_fn=_ocr_jpeg)
    except Exception as e:
        logger.exception("Imaging failed for %s", doc.bates_begin)
        doc.imaging_status = "failed"
        doc.imaging_error = str(e)[:500] or e.__class__.__name__
        await db.commit()
        return
    # A box saved while pages were being prepared was drawn on the old page
    # numbering; never put new pages under it.
    late = (await db.execute(
        select(func.count(Redaction.id)).where(Redaction.document_id == doc_id)
    )).scalar() or 0
    if late:
        doc.imaging_status = "failed"
        doc.imaging_error = "redactions were added while pages were being prepared"
        await db.commit()
        return
    doc.image_paths = image_paths
    doc.ocr_paths = ocr_paths
    doc.page_count = count
    doc.imaged_from = kind
    doc.header_regions = regions
    doc.imaging_status = "done"
    doc.imaging_error = None
    await db.commit()
