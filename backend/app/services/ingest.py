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


INGEST_BATCH_SIZE = 25


def bootstrap_ingest_source(production_id: int) -> tuple[list[dict], dict[str, list[str]]]:
    """Download and parse the DAT and OPT files for a production.

    Called both by /ingest/process (to count records) and by each
    Cloud Task worker (to get the records to process). Cheap enough
    to re-run per batch — DAT/OPT are small and parsing is fast.
    """
    import tempfile

    from app.services.storage import download_file, list_files

    prefix = f"productions/{production_id}/raw/"
    data_files = list_files(f"{prefix}DATA/")
    dat_remote = next((f for f in data_files if f.lower().endswith(".dat")), None)
    opt_remote = next((f for f in data_files if f.lower().endswith(".opt")), None)

    if not dat_remote:
        raise FileNotFoundError("No .dat file found in uploaded DATA/ folder")
    if not opt_remote:
        raise FileNotFoundError("No .opt file found in uploaded DATA/ folder")

    tmp_dir = tempfile.mkdtemp(prefix=f"ingest_parse_{production_id}_")
    dat_local = os.path.join(tmp_dir, "data.dat")
    opt_local = os.path.join(tmp_dir, "data.opt")
    download_file(dat_remote, dat_local)
    download_file(opt_remote, opt_local)

    records = parse_dat(dat_local)
    opt_pages = parse_opt(opt_local)
    return records, opt_pages


def process_ingest_record(
    production_id: int,
    record: dict,
    opt_pages: dict[str, list[str]],
    converted_tmp: str,
    errors: list[str],
) -> Document | None:
    """Turn a single DAT record into a Document.

    Downloads text + images from Firebase Storage, converts TIFFs to
    JPEG, and returns an unsaved Document instance. Returns None if
    the record has no Bates (skipped).
    """
    from PIL import Image

    from app.services.storage import download_to_temp, get_download_bytes, upload_file

    prefix = f"productions/{production_id}/raw/"
    bates_begin = record.get("Begin Bates", "").strip()
    bates_end = record.get("End Bates", "").strip()
    page_count_str = record.get("Page Count", "1").strip()
    text_link = record.get("Text Link", "").strip()
    native_link = record.get("Native Link", "").strip()

    if not bates_begin:
        return None

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
    jpeg_storage_paths: list[str] = []
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

    return Document(
        production_id=production_id,
        bates_begin=bates_begin,
        bates_end=bates_end,
        page_count=page_count,
        metadata_=metadata,
        text_content=text_content,
        native_path=native_storage_path,
        image_paths=jpeg_storage_paths,
    )


async def ingest_batch(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    start_idx: int,
    end_idx: int,
) -> None:
    """Process records[start_idx:end_idx] for an ingest job.

    Safe to retry — documents that already exist (by production_id +
    bates_begin) are skipped. Increments job.processed_files for each
    document actually inserted.
    """
    import shutil
    import tempfile
    from datetime import datetime, timezone

    from sqlalchemy import func

    from app.models import IngestJob

    job = await db.get(IngestJob, job_id)
    if not job:
        return

    records, opt_pages = bootstrap_ingest_source(production_id)
    slice_records = records[start_idx:end_idx]

    # Collect Bates numbers already present so retried batches are idempotent
    bates_in_slice = [
        r.get("Begin Bates", "").strip()
        for r in slice_records
        if r.get("Begin Bates", "").strip()
    ]
    existing: set[str] = set()
    if bates_in_slice:
        result = await db.execute(
            select(Document.bates_begin).where(
                Document.production_id == production_id,
                Document.bates_begin.in_(bates_in_slice),
            )
        )
        existing = {row[0] for row in result.all()}

    tmp_dir = tempfile.mkdtemp(prefix=f"ingest_batch_{production_id}_{start_idx}_")
    converted_tmp = os.path.join(tmp_dir, "converted")
    os.makedirs(converted_tmp, exist_ok=True)
    errors: list[str] = list(job.errors or [])

    try:
        for record in slice_records:
            bates_begin = record.get("Begin Bates", "").strip()
            if not bates_begin:
                errors.append("Row: missing Begin Bates")
                continue
            if bates_begin in existing:
                # Already committed on a previous attempt — skip
                continue
            try:
                doc = process_ingest_record(
                    production_id, record, opt_pages, converted_tmp, errors
                )
                if doc is None:
                    continue
                db.add(doc)
                await db.flush()
                # Update tsvector for the newly inserted doc
                await db.execute(
                    text(
                        "UPDATE documents SET text_search_vector = "
                        "to_tsvector('english', COALESCE(text_content, '')) "
                        "WHERE id = :id"
                    ),
                    {"id": doc.id},
                )
                # Increment progress atomically so parallel batches don't clobber
                await db.execute(
                    text(
                        "UPDATE ingest_jobs SET processed_files = processed_files + 1 "
                        "WHERE id = :jid"
                    ),
                    {"jid": job_id},
                )
                await db.commit()
            except Exception as e:
                logger.exception("Failed to process record %s", bates_begin)
                errors.append(f"{bates_begin}: {e}")
                await db.rollback()

        # Persist any error messages collected in this batch
        await db.execute(
            text("UPDATE ingest_jobs SET errors = :errs WHERE id = :jid"),
            {"errs": errors, "jid": job_id},
        )
        await db.commit()

        # If this batch pushed us to completion, finalize the job
        await db.refresh(job)
        if job.processed_files >= job.total_files and job.status == "processing":
            # AI titles are best-effort
            if settings.anthropic_api_key:
                try:
                    result = await db.execute(
                        select(Document).where(
                            Document.production_id == production_id,
                            Document.title.is_(None),
                        )
                    )
                    docs_for_titles = list(result.scalars().all())
                    texts_for_titles = [
                        (str(d.id), d.text_content) for d in docs_for_titles
                    ]
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
            job.errors = errors
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def ingest_from_storage(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    production_name: str,
) -> None:
    """In-process fallback ingest used when Cloud Tasks isn't configured.

    Runs the entire ingest inline as one long operation. Fine for local
    dev but unreliable on Cloud Run for long ingests because BackgroundTasks
    can be killed by container scale-down.
    """
    from datetime import datetime, timezone

    from app.models import IngestJob

    job = await db.get(IngestJob, job_id)
    if not job:
        return

    try:
        records, _ = bootstrap_ingest_source(production_id)
        job.total_files = len(records)
        await db.commit()

        for start in range(0, len(records), INGEST_BATCH_SIZE):
            await ingest_batch(
                db, job_id, production_id, start, start + INGEST_BATCH_SIZE
            )

        # ingest_batch auto-finalizes when processed_files >= total_files
    except Exception as e:
        logger.exception("Inline ingest failed")
        job = await db.get(IngestJob, job_id)
        if job:
            job.status = "failed"
            job.errors = (job.errors or []) + [str(e)]
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
