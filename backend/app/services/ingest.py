"""Production ingest pipeline."""

import logging
import os
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, Production
from app.services.images import convert_document_images
from app.services.ai import generate_titles_batch
from app.utils.parsers import parse_dat, parse_opt

logger = logging.getLogger(__name__)

# Known DAT field names that map to document columns
FIELD_MAP = {
    "Begin Bates": "bates_begin",
    "End Bates": "bates_end",
    "Page Count": "page_count",
    "Text Link": "text_link",
    "Native Link": "native_link",
}


async def ingest_production(
    db: AsyncSession,
    production_name: str,
    production_root: str,
    description: str = "",
    owner_id: str | None = None,
) -> dict:
    """Ingest a full production from disk into the database.

    Returns a summary dict with counts and any validation errors.
    """
    production_root = os.path.abspath(production_root)
    errors: list[str] = []

    # Find DAT and OPT files
    data_dir = os.path.join(production_root, "DATA")
    dat_files = list(Path(data_dir).glob("*.dat")) if os.path.isdir(data_dir) else []
    opt_files = list(Path(data_dir).glob("*.opt")) if os.path.isdir(data_dir) else []

    if not dat_files:
        raise FileNotFoundError(f"No .dat file found in {data_dir}")
    if not opt_files:
        raise FileNotFoundError(f"No .opt file found in {data_dir}")

    dat_path = str(dat_files[0])
    opt_path = str(opt_files[0])

    # Parse files
    dat_records = parse_dat(dat_path)
    opt_pages = parse_opt(opt_path)

    logger.info(f"Parsed {len(dat_records)} documents from DAT, {len(opt_pages)} documents from OPT")

    # Create or get production
    production = Production(name=production_name, description=description, owner_id=owner_id)
    db.add(production)
    await db.flush()

    # Set up image output directory
    converted_dir = os.path.join(settings.storage_root, "converted", production_name)
    os.makedirs(converted_dir, exist_ok=True)

    documents = []
    for i, record in enumerate(dat_records):
        bates_begin = record.get("Begin Bates", "").strip()
        bates_end = record.get("End Bates", "").strip()
        page_count_str = record.get("Page Count", "1").strip()
        text_link = record.get("Text Link", "").strip()
        native_link = record.get("Native Link", "").strip()

        if not bates_begin:
            errors.append(f"Row {i+1}: missing Begin Bates")
            continue

        page_count = int(page_count_str) if page_count_str.isdigit() else 1

        # Read extracted text
        text_content = None
        if text_link:
            text_path = os.path.join(production_root, text_link)
            if os.path.exists(text_path):
                with open(text_path, "r", encoding="utf-8-sig", errors="replace") as f:
                    text_content = f.read()
                # Strip null bytes — Postgres rejects 0x00 in text columns
                text_content = text_content.replace("\x00", "")
            else:
                errors.append(f"{bates_begin}: text file not found: {text_link}")

        # Get image paths from OPT and convert to JPEG
        raw_image_paths = opt_pages.get(bates_begin, [])
        if not raw_image_paths:
            errors.append(f"{bates_begin}: no images found in OPT file")

        jpeg_paths = convert_document_images(
            raw_image_paths, production_root, converted_dir
        )

        # Build metadata dict from all DAT fields (for flexibility)
        metadata = {}
        for key, value in record.items():
            if key not in FIELD_MAP and value:
                metadata[key] = value

        doc = Document(
            production_id=production.id,
            bates_begin=bates_begin,
            bates_end=bates_end,
            page_count=page_count,
            metadata_=metadata,
            text_content=text_content,
            native_path=native_link if native_link else None,
            image_paths=jpeg_paths,
        )
        documents.append(doc)

    db.add_all(documents)
    await db.flush()

    # Update tsvector for all new documents
    await db.execute(
        text(
            "UPDATE documents SET text_search_vector = to_tsvector('english', COALESCE(text_content, '')) "
            "WHERE production_id = :pid"
        ),
        {"pid": production.id},
    )

    # Generate AI titles for documents with text content
    if settings.anthropic_api_key:
        logger.info("Generating AI titles for %d documents...", len(documents))
        texts = [(str(doc.id), doc.text_content) for doc in documents]
        titles = await generate_titles_batch(texts)
        for doc in documents:
            title = titles.get(str(doc.id))
            if title:
                doc.title = title
        await db.flush()
        logger.info("AI titles generated for %d documents", sum(1 for t in titles.values() if t))

    await db.commit()

    return {
        "production_id": production.id,
        "production_name": production_name,
        "documents_ingested": len(documents),
        "errors": errors,
        "error_count": len(errors),
    }


async def ingest_from_storage(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    production_name: str,
) -> None:
    """Process uploaded production files from Firebase Storage."""
    import shutil
    import tempfile
    from datetime import datetime, timezone

    from PIL import Image

    from app.models import IngestJob
    from app.services.storage import (
        download_file,
        download_to_temp,
        get_download_bytes,
        list_files,
        upload_file,
    )

    job = await db.get(IngestJob, job_id)
    if not job:
        return

    prefix = f"productions/{production_id}/raw/"
    errors: list[str] = []
    tmp_dir = tempfile.mkdtemp(prefix=f"ingest_{production_id}_")

    try:
        # Download DAT and OPT files
        data_files = list_files(f"{prefix}DATA/")
        dat_remote = next((f for f in data_files if f.lower().endswith(".dat")), None)
        opt_remote = next((f for f in data_files if f.lower().endswith(".opt")), None)

        if not dat_remote:
            raise FileNotFoundError("No .dat file found in uploaded DATA/ folder")
        if not opt_remote:
            raise FileNotFoundError("No .opt file found in uploaded DATA/ folder")

        dat_local = os.path.join(tmp_dir, "data.dat")
        opt_local = os.path.join(tmp_dir, "data.opt")
        download_file(dat_remote, dat_local)
        download_file(opt_remote, opt_local)

        dat_records = parse_dat(dat_local)
        opt_pages = parse_opt(opt_local)

        job.total_files = len(dat_records)
        await db.commit()

        converted_tmp = os.path.join(tmp_dir, "converted")
        os.makedirs(converted_tmp, exist_ok=True)

        # Commit documents incrementally so partial progress survives a
        # container restart / scale-down. Without this, accumulating all
        # documents in memory and committing at the end means a killed
        # background task loses every document it processed.
        BATCH_SIZE = 25
        pending_batch: list[Document] = []
        all_committed_ids: list[str] = []

        async def flush_batch() -> None:
            nonlocal pending_batch
            if not pending_batch:
                return
            db.add_all(pending_batch)
            await db.flush()
            for d in pending_batch:
                all_committed_ids.append(str(d.id))
            job.processed_files = len(all_committed_ids)
            job.errors = errors.copy()
            await db.commit()
            pending_batch = []

        for i, record in enumerate(dat_records):
            bates_begin = record.get("Begin Bates", "").strip()
            bates_end = record.get("End Bates", "").strip()
            page_count_str = record.get("Page Count", "1").strip()
            text_link = record.get("Text Link", "").strip()
            native_link = record.get("Native Link", "").strip()

            if not bates_begin:
                errors.append(f"Row {i+1}: missing Begin Bates")
                continue

            page_count = int(page_count_str) if page_count_str.isdigit() else 1

            # Read text from Firebase Storage
            text_content = None
            if text_link:
                text_remote = f"{prefix}{text_link.replace(chr(92), '/')}"
                try:
                    text_bytes = get_download_bytes(text_remote)
                    text_content = text_bytes.decode("utf-8-sig", errors="replace")
                    text_content = text_content.replace("\x00", "")
                except Exception:
                    errors.append(f"{bates_begin}: text file not found: {text_link}")

            # Convert images
            raw_image_paths = opt_pages.get(bates_begin, [])
            jpeg_storage_paths = []
            for rel_path in raw_image_paths:
                remote_tiff = f"{prefix}{rel_path.replace(chr(92), '/')}"
                try:
                    tiff_local = download_to_temp(remote_tiff, suffix=".tif")
                    stem = Path(rel_path).stem
                    jpeg_local = os.path.join(converted_tmp, f"{stem}.jpg")
                    with Image.open(tiff_local) as img:
                        if img.mode not in ("RGB", "L"):
                            img = img.convert("RGB")
                        img.save(jpeg_local, "JPEG", quality=85)
                    os.unlink(tiff_local)

                    jpeg_remote = f"productions/{production_id}/converted/{stem}.jpg"
                    upload_file(jpeg_local, jpeg_remote, content_type="image/jpeg")
                    jpeg_storage_paths.append(jpeg_remote)
                except Exception as e:
                    errors.append(f"{bates_begin}: image conversion failed: {rel_path}: {e}")
                    jpeg_storage_paths.append("")

            native_storage_path = None
            if native_link:
                native_storage_path = f"{prefix}{native_link.replace(chr(92), '/')}"

            metadata = {}
            for key, value in record.items():
                if key not in FIELD_MAP and value:
                    metadata[key] = value

            doc = Document(
                production_id=production_id,
                bates_begin=bates_begin,
                bates_end=bates_end,
                page_count=page_count,
                metadata_=metadata,
                text_content=text_content,
                native_path=native_storage_path,
                image_paths=jpeg_storage_paths,
            )
            pending_batch.append(doc)

            if len(pending_batch) >= BATCH_SIZE:
                await flush_batch()

        # Flush any remaining documents
        await flush_batch()

        # Update full-text search vectors for everything we just inserted
        await db.execute(
            text(
                "UPDATE documents SET text_search_vector = to_tsvector('english', COALESCE(text_content, '')) "
                "WHERE production_id = :pid"
            ),
            {"pid": production_id},
        )
        await db.commit()

        # Generate AI titles — wrap in try/except so title generation
        # failures (API errors, rate limits, timeouts) don't fail the
        # whole ingest. Documents are already saved at this point.
        if settings.anthropic_api_key:
            try:
                # Reload documents for title generation
                result = await db.execute(
                    select(Document).where(Document.production_id == production_id)
                )
                docs_for_titles = list(result.scalars().all())
                texts_for_titles = [(str(d.id), d.text_content) for d in docs_for_titles]
                titles = await generate_titles_batch(texts_for_titles)
                for d in docs_for_titles:
                    t = titles.get(str(d.id))
                    if t:
                        d.title = t
                await db.commit()
            except Exception as e:
                logger.exception("AI title generation failed")
                errors.append(f"AI title generation skipped: {e}")

        job.status = "complete"
        job.processed_files = len(all_committed_ids)
        job.errors = errors
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()

    except Exception as e:
        job.status = "failed"
        job.errors = errors + [str(e)]
        await db.commit()
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
