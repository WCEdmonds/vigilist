"""Ingest a folder of loose native files (the `native` source_format).

Reuses the PDF page-render path for PDFs and the Python extraction dispatcher
for Office/text/image files. Per-file failures become error rows; the batch
never aborts on one bad file.
"""

import asyncio
import hashlib
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, IngestJob, Production
from app.services.extractors import extract
from app.services.storage import get_download_bytes, list_files

logger = logging.getLogger(__name__)


def list_native_sources(production_id: int) -> list[dict]:
    """List ALL uploaded files for a production, sorted deterministically.

    Mirrors ``list_pdf_sources`` but is not filtered by extension.
    """
    prefix = f"productions/{production_id}/raw/"
    all_files = sorted(list_files(prefix))
    items: list[dict] = []
    for path in all_files:
        relative_path = path[len(prefix):] if path.startswith(prefix) else path
        items.append(
            {
                "storage_path": path,
                "relative_path": relative_path,
                "filename": os.path.basename(relative_path),
            }
        )
    return items


def process_native_record(
    custodian: str | None,
    production_id: int,
    item: dict,
    global_index: int,
    prefix: str,
    errors: list[str],
) -> Document | None:
    """Turn one uploaded native file into an unsaved Document. Never raises."""
    from app.services.ingest_pdf import (
        _ocr_jpeg,
        looks_like_bates_stub,
        process_pdf_record,
    )

    control_number = f"{prefix} {global_index + 1:06d}"
    storage_path = item["storage_path"]
    relative_path = item["relative_path"]
    filename = item["filename"]
    ext = os.path.splitext(filename)[1].lower()

    try:
        data = get_download_bytes(storage_path)
    except Exception as e:
        errors.append(f"{control_number}: could not download {relative_path}: {e}")
        return None

    sha256 = hashlib.sha256(data).hexdigest()

    # PDFs: reuse the page-render + OCR path, then stamp SP4a fields.
    if ext == ".pdf":
        try:
            doc = process_pdf_record(production_id, item, global_index, prefix, errors)
        except Exception as e:
            errors.append(f"{control_number}: failed to render {relative_path}: {e}")
            return None
        if doc is None:
            return None
        doc.file_hash_sha256 = sha256
        doc.file_name = filename
        doc.file_type = "pdf"
        doc.source_path = relative_path
        doc.custodian = custodian
        return doc

    # Everything else: dispatch text extraction (images use Vision OCR).
    res = extract(filename, data, ocr_fn=_ocr_jpeg)

    folder = os.path.dirname(relative_path)
    metadata = {"File Name": filename}
    if folder:
        metadata["Folder"] = folder

    stem = os.path.splitext(filename)[0]
    title = None if looks_like_bates_stub(stem) else stem[:200]

    return Document(
        production_id=production_id,
        bates_begin=control_number,
        bates_end=control_number,
        page_count=1,
        metadata_=metadata,
        title=title,
        text_content=res.text or None,
        native_path=storage_path,
        image_paths=[],
        file_name=filename,
        file_type=res.file_type,
        source_path=relative_path,
        custodian=custodian,
        file_hash_sha256=sha256,
        extraction_status=res.extraction_status,
        extraction_error=res.extraction_error,
    )


async def ingest_native_batch(
    db: AsyncSession, job_id: str, production_id: int, start_idx: int, end_idx: int
) -> None:
    """Process one batch of native files. Mirrors ``ingest_pdf_batch``."""
    from app.services.ingest import (
        _finalize_job_if_done,
        _incr_skipped,
        _persist_document,
        _persist_job_errors,
    )
    from app.services.ingest_pdf import derive_bates_prefix

    job = await db.get(IngestJob, job_id)
    if not job:
        return
    production = await db.get(Production, production_id)
    prefix = derive_bates_prefix(production.name if production else "")
    custodian = (job.field_mapping or {}).get("custodian")

    items = list_native_sources(production_id)
    errors: list[str] = list(job.errors or [])

    slice_pairs = [(idx, items[idx]) for idx in range(start_idx, min(end_idx, len(items)))]
    storage_paths = [item["storage_path"] for _, item in slice_pairs]
    existing: set[str] = set()
    if storage_paths:
        result = await db.execute(
            select(Document.native_path).where(
                Document.production_id == production_id,
                Document.native_path.in_(storage_paths),
            )
        )
        existing = {row[0] for row in result.all()}

    for global_index, item in slice_pairs:
        control_number = f"{prefix} {global_index + 1:06d}"
        if item["storage_path"] in existing:
            await _incr_skipped(db, job_id)
            continue
        try:
            doc = await asyncio.to_thread(
                process_native_record,
                custodian, production_id, item, global_index, prefix, errors,
            )
            if doc is None:
                await _incr_skipped(db, job_id)
                continue
            await _persist_document(db, job_id, doc)
        except Exception as e:
            logger.exception("Failed to process native file %s", item.get("relative_path"))
            errors.append(f"{control_number}: {e}")
            await db.rollback()
            await _incr_skipped(db, job_id)

    await _persist_job_errors(db, job_id, errors)
    await _finalize_job_if_done(db, job, production_id, errors)
