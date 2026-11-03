"""Backfill OCR for image documents that were loaded without text.

Before the inline-image rule was fixed, every cid:-referenced image skipped
OCR. This walks a production's image documents with no text (keyset by id, no
blobs loaded for counting), applies the same ``should_ocr_image`` rule per
document in the worker, OCRs via Cloud Vision, and refreshes the tsvector and
embeddings. Idempotent: documents that already have text are skipped.
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import func, select, text

from app.models import Document
from app.services.embeddings import embed_documents
from app.services.extractors import should_ocr_image

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
USD_PER_IMAGE = 0.0015


def download_bytes(path: str) -> bytes:
    from app.services.storage import get_download_bytes
    return get_download_bytes(path)


def ocr_bytes(data: bytes) -> str:
    from app.services.ocr import ocr_image_vision_bytes
    return (ocr_image_vision_bytes(data) or "").strip()


def _candidate_filter(production_id: int):
    return (
        Document.production_id == production_id,
        Document.file_type == "image",
        Document.native_path.is_not(None),
        Document.native_path != "",
        (Document.text_content.is_(None)) | (Document.text_content == ""),
    )


async def count_candidates(db, production_id: int) -> int:
    return int(await db.scalar(
        select(func.count()).select_from(Document).where(*_candidate_filter(production_id))
    ) or 0)


async def run_batch(db, production_id: int, after_id: str | None,
                    limit: int = BATCH_SIZE) -> dict:
    """Process one keyset page. ``next_after`` is the last id seen; ``done``
    is True when the page was short."""
    from app.services import ingest as ingest_service

    q = select(Document).where(*_candidate_filter(production_id))
    if after_id:
        q = q.where(Document.id > UUID(str(after_id)))
    docs = list((await db.execute(q.order_by(Document.id).limit(limit))).scalars().all())

    stats = {"ocrd": 0, "skipped_tiny": 0, "skipped_has_text": 0, "failed": 0}
    gained: list = []
    for doc in docs:
        try:
            if doc.text_content:
                stats["skipped_has_text"] += 1
                continue
            data = await asyncio.to_thread(download_bytes, doc.native_path)
            if not should_ocr_image(doc.native_path, data, inline=False):
                stats["skipped_tiny"] += 1
                continue
            txt = await asyncio.to_thread(ocr_bytes, data)
            if not txt:
                continue
            doc.text_content = txt
            await db.execute(
                text("UPDATE documents SET text_search_vector = "
                     f"{ingest_service.tsvector_sql(':txt')} WHERE id = :id"),
                {"txt": txt, "id": doc.id},
            )
            await db.commit()
            gained.append(doc.id)
            stats["ocrd"] += 1
        except Exception:
            logger.exception("OCR backfill failed for doc %s", getattr(doc, "bates_begin", doc.id))
            await db.rollback()
            stats["failed"] += 1

    if gained:
        try:
            await embed_documents(db, gained)
        except Exception:
            logger.exception("OCR backfill embedding failed for production %d", production_id)

    logger.info("OCR backfill production %d after %s: %s", production_id, after_id, stats)
    return {**stats, "next_after": str(docs[-1].id) if docs else after_id,
            "done": len(docs) < limit}
