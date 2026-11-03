"""Backfill OCR for image documents that were loaded without text.

Before the inline-image rule was fixed, every cid:-referenced image skipped
OCR. This walks a production's image documents with no text (keyset by id, no
blobs loaded for counting), applies the same ``should_ocr_image`` rule per
document in the worker, OCRs via Cloud Vision, and refreshes the tsvector and
embeddings. Idempotent: documents that already have text are skipped.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import DateTime, and_, cast, func, or_, select, text

from app.models import Document
from app.services.embeddings import embed_documents
from app.services.extractors import should_ocr_image

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
USD_PER_IMAGE = 0.0015
STALE_AFTER = timedelta(minutes=30)
MARKER = "ocr_backfill"


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
        # A doc stamped by an earlier run is never re-billed, except a
        # 'pending' claim old enough that its worker must have died.
        or_(
            ~Document.metadata_.has_key(MARKER),
            and_(
                Document.metadata_[MARKER]["result"].astext == "pending",
                cast(Document.metadata_[MARKER]["at"].astext, DateTime(timezone=True))
                < func.now() - STALE_AFTER,
            ),
        ),
    )


async def count_candidates(db, production_id: int) -> int:
    return int(await db.scalar(
        select(func.count()).select_from(Document).where(*_candidate_filter(production_id))
    ) or 0)


_STAMP_SQL = (
    "UPDATE documents SET metadata = jsonb_set(coalesce(metadata, '{}'::jsonb), "
    "'{ocr_backfill}', CAST(:v AS jsonb)) WHERE id = :id"
)


def _stamp_value(result: str) -> str:
    return json.dumps({"at": datetime.now(timezone.utc).isoformat(), "result": result})


async def _stamp(db, doc_id, result: str) -> None:
    await db.execute(text(_STAMP_SQL), {"v": _stamp_value(result), "id": doc_id})
    await db.commit()


async def run_batch(db, production_id: int, after_id: str | None,
                    limit: int = BATCH_SIZE) -> dict:
    """Process one keyset page. ``next_after`` is the last id seen; ``done``
    is True when the page was short.

    Works on plain column rows and Core statements only: a rollback after one
    doc's failure must not leave later iterations touching expired ORM state.
    """
    from app.services import ingest as ingest_service

    q = select(Document.id, Document.native_path, Document.bates_begin,
               Document.text_content).where(*_candidate_filter(production_id))
    if after_id:
        q = q.where(Document.id > UUID(str(after_id)))
    rows = [(r.id, r.native_path, r.bates_begin, r.text_content)
            for r in (await db.execute(q.order_by(Document.id).limit(limit))).all()]
    await db.commit()  # release the connection before the slow per-doc work

    stats = {"ocrd": 0, "empty": 0, "skipped_tiny": 0, "skipped_has_text": 0,
             "failed": 0, "embed_failed": 0}
    gained: list = []
    for doc_id, path, bates, existing in rows:
        try:
            if existing:
                stats["skipped_has_text"] += 1
                continue
            # Claim before the billed call so a concurrent chain skips it.
            await _stamp(db, doc_id, "pending")
            data = await asyncio.to_thread(download_bytes, path)
            if not should_ocr_image(path, data, inline=False):
                await _stamp(db, doc_id, "tiny")
                stats["skipped_tiny"] += 1
                continue
            txt = ingest_service._strip_nul(await asyncio.to_thread(ocr_bytes, data))
            if not txt:
                await _stamp(db, doc_id, "empty")
                stats["empty"] += 1
                continue
            await db.execute(
                text("UPDATE documents SET text_content = :txt, "
                     f"text_search_vector = {ingest_service.tsvector_sql(':txt')}, "
                     "metadata = jsonb_set(coalesce(metadata, '{}'::jsonb), "
                     "'{ocr_backfill}', CAST(:v AS jsonb)) WHERE id = :id"),
                {"txt": txt, "v": _stamp_value("ocr"), "id": doc_id},
            )
            await db.commit()
            gained.append(doc_id)
            stats["ocrd"] += 1
        except Exception:
            logger.exception("OCR backfill failed for doc %s", bates or doc_id)
            stats["failed"] += 1
            try:
                await db.rollback()
                await _stamp(db, doc_id, "error")
            except Exception:
                logger.exception("OCR backfill could not stamp error on doc %s", doc_id)
                await db.rollback()

    if gained:
        for attempt in (1, 2):
            try:
                await embed_documents(db, gained)
                break
            except Exception:
                logger.exception("OCR backfill embedding failed (attempt %d)", attempt)
                await db.rollback()
        else:
            # Text is saved with no chunks, so embed_production_documents
            # (text and no chunks) picks these up; the stamp records why.
            stats["embed_failed"] = len(gained)
            for doc_id in gained:
                try:
                    await _stamp(db, doc_id, "embed_error")
                except Exception:
                    await db.rollback()

    logger.info("OCR backfill production %d after %s: %s", production_id, after_id, stats)
    return {**stats, "next_after": str(rows[-1][0]) if rows else after_id,
            "done": len(rows) < limit}


# --- per-production run marker / progress (productions.ai_pipeline_status) ---

_COUNTERS = ("done", "ocr", "empty", "tiny", "errors")


async def _read_marker(db, production_id: int) -> dict:
    v = await db.scalar(text("SELECT ai_pipeline_status -> 'ocr_backfill' FROM productions "
                             "WHERE id = :p"), {"p": production_id})
    return dict(v) if isinstance(v, dict) else {}


async def _write_marker(db, production_id: int, marker: dict) -> None:
    await db.execute(
        text("UPDATE productions SET ai_pipeline_status = jsonb_set("
             "coalesce(ai_pipeline_status, '{}'::jsonb), '{ocr_backfill}', "
             "CAST(:v AS jsonb)) WHERE id = :p"),
        {"v": json.dumps(marker), "p": production_id},
    )
    await db.commit()


async def try_start_run(db, production_id: int) -> bool:
    """Atomically claim the run: False if a chain touched it < 30 min ago."""
    now = datetime.now(timezone.utc)
    marker = {"status": "running", "started_at": now.isoformat(),
              "updated_at": now.isoformat(), **{k: 0 for k in _COUNTERS}}
    res = await db.execute(
        text("UPDATE productions SET ai_pipeline_status = jsonb_set("
             "coalesce(ai_pipeline_status, '{}'::jsonb), '{ocr_backfill}', "
             "CAST(:v AS jsonb)) WHERE id = :p AND NOT ("
             "coalesce(ai_pipeline_status->'ocr_backfill'->>'status', '') = 'running' "
             "AND (ai_pipeline_status->'ocr_backfill'->>'updated_at')::timestamptz "
             "> now() - interval '30 minutes')"),
        {"v": json.dumps(marker), "p": production_id},
    )
    await db.commit()
    return res.rowcount == 1


async def record_progress(db, production_id: int, res: dict) -> None:
    m = await _read_marker(db, production_id)
    m.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    m["done"] = m.get("done", 0) + res["ocrd"] + res["empty"] + res["skipped_tiny"] + res["failed"]
    m["ocr"] = m.get("ocr", 0) + res["ocrd"]
    m["empty"] = m.get("empty", 0) + res["empty"]
    m["tiny"] = m.get("tiny", 0) + res["skipped_tiny"]
    m["errors"] = m.get("errors", 0) + res["failed"]
    m["updated_at"] = datetime.now(timezone.utc).isoformat()
    m["status"] = "done" if res["done"] else "running"
    await _write_marker(db, production_id, m)
