"""Production ingest pipeline."""

import asyncio
import json
import logging
import os
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, Production
from app.services.field_mapping import match_aliases
from app.services.images import convert_document_images
from app.services.ai import generate_titles_batch
from app.services.metadata_normalize import derive_file_type, promote_record
from app.utils.parsers import parse_dat, parse_opt

logger = logging.getLogger(__name__)

# Strong references to detached local (non-Cloud-Tasks) pipeline tasks — without
# this, asyncio only holds a weak reference to a fire-and-forget task, and it
# can be garbage-collected mid-run.
_pipeline_tasks: set = set()

# Known DAT field names that map to document columns
FIELD_MAP = {
    "Begin Bates": "bates_begin",
    "End Bates": "bates_end",
    "Page Count": "page_count",
    "Text Link": "text_link",
    "Native Link": "native_link",
}

FIELD_MAP_REVERSED = {v: k for k, v in FIELD_MAP.items()}


def _effective_mapping(record_keys, field_mapping: dict | None) -> dict:
    """Use the confirmed field_mapping if present; else fall back to alias
    matching over the record's own columns (keeps ingest working without an
    explicit mapping, e.g. the legacy/inline path)."""
    if field_mapping:
        return field_mapping
    return match_aliases(list(record_keys))


def _stamp_source(doc, job) -> None:
    """Stamp job-level source designation; a load-file-mapped source_party wins."""
    fm = job.field_mapping or {}
    if getattr(doc, "source_party", None) is None:
        doc.source_party = fm.get("source_party")
    if getattr(doc, "source_type", None) is None:
        doc.source_type = fm.get("source_type")


def _apply_metadata(doc, record: dict, field_mapping: dict | None) -> None:
    """Promote typed metadata onto a freshly built Document."""
    mapping = _effective_mapping(record.keys(), field_mapping)
    typed, leftover = promote_record(record, mapping)
    for field, value in typed.items():
        setattr(doc, field, value)
    if not doc.file_type:
        doc.file_type = derive_file_type(doc.file_name or doc.native_path)
    # Preserve original values for everything not structural.
    doc.metadata_ = leftover


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

        nl = record.get(FIELD_MAP_REVERSED.get("native_link", "Native Link"), "") or ""
        file_name = os.path.basename(nl.replace("\\", "/")) if nl else None
        doc = Document(
            production_id=production.id,
            bates_begin=bates_begin,
            bates_end=bates_end,
            page_count=page_count,
            metadata_={},
            text_content=text_content,
            native_path=native_link if native_link else None,
            image_paths=jpeg_paths,
            file_name=file_name,
        )
        _apply_metadata(doc, record, None)
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

    # Generate chunk embeddings so semantic search / clustering / duplicate
    # detection work on this production. Best-effort — never fails the ingest.
    try:
        from app.services.embeddings import embed_production_documents
        await embed_production_documents(db, production.id)
    except Exception as e:
        logger.exception("Embedding generation failed")
        errors.append(f"Embedding generation skipped: {e}")

    return {
        "production_id": production.id,
        "production_name": production_name,
        "documents_ingested": len(documents),
        "errors": errors,
        "error_count": len(errors),
    }


INGEST_BATCH_SIZE = 25


def compute_control_offset(bates_values: list, prefix: str) -> int:
    """Max numeric tail among existing '{prefix} NNNNNN' control numbers.

    Later loads continue the sequence instead of restarting at 000001 (which
    would collide with the (production_id, bates_begin) unique key and be
    silently skipped as "already ingested").
    """
    import re

    pat = re.compile(rf"^{re.escape(prefix)} (\d+)$")
    best = 0
    for b in bates_values:
        m = pat.match(b or "")
        if m:
            best = max(best, int(m.group(1)))
    return best


def _download_dat_to_temp(production_id: int, load_prefix: str | None = None) -> str:
    """Download the production's DAT load file to a temp path and return it.

    Reused by both bootstrap_ingest_source and analyze_load_file so we never
    duplicate the Firebase Storage access logic.
    """
    import tempfile

    from app.services.storage import download_file, list_files

    prefix = f"productions/{production_id}/raw/{load_prefix or ''}"
    data_files = list_files(f"{prefix}DATA/")
    dat_remote = next((f for f in data_files if f.lower().endswith(".dat")), None)

    if not dat_remote:
        raise FileNotFoundError("No .dat file found in uploaded DATA/ folder")

    tmp_dir = tempfile.mkdtemp(prefix=f"ingest_dat_{production_id}_")
    dat_local = os.path.join(tmp_dir, "data.dat")
    download_file(dat_remote, dat_local)
    return dat_local


def bootstrap_ingest_source(production_id: int, load_prefix: str | None = None) -> tuple[list[dict], dict[str, list[str]]]:
    """Download and parse the DAT and OPT files for a production.

    Called both by /ingest/process (to count records) and by each
    Cloud Task worker (to get the records to process). Cheap enough
    to re-run per batch — DAT/OPT are small and parsing is fast.
    """
    import tempfile

    from app.services.storage import download_file, list_files

    prefix = f"productions/{production_id}/raw/{load_prefix or ''}"
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


def analyze_load_file(production_id: int, load_prefix: str | None = None) -> dict:
    """Parse the uploaded load file and propose a column mapping.

    Downloads the DAT file from Firebase Storage (reusing _download_dat_to_temp),
    parses it with parse_loadfile, and runs build_proposed_mapping.
    Never raises on AI failure — build_proposed_mapping handles that gracefully.
    """
    from app.utils.loadfile import parse_loadfile
    from app.services.field_mapping import build_proposed_mapping

    dat_path = _download_dat_to_temp(production_id, load_prefix)
    parsed = parse_loadfile(dat_path)
    columns = build_proposed_mapping(parsed.headers, parsed.sample_rows)
    return {
        "format": {
            "encoding": parsed.encoding,
            "delimiter": parsed.delimiter,
        },
        "columns": columns,
        "sample_rows": parsed.sample_rows,
        "total_rows": parsed.total_rows,
    }


def process_ingest_record(
    production_id: int,
    record: dict,
    opt_pages: dict[str, list[str]],
    converted_tmp: str,
    errors: list[str],
    field_mapping: dict | None = None,
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

    # Run Cloud Vision OCR on converted images for higher-quality text
    vision_text_parts: list[str] = []
    for jpeg_path in jpeg_storage_paths:
        if not jpeg_path:
            continue
        try:
            jpeg_bytes = get_download_bytes(jpeg_path)
            from app.services.ocr import ocr_image_vision_bytes
            page_text = ocr_image_vision_bytes(jpeg_bytes)
            if page_text:
                vision_text_parts.append(page_text)
        except Exception as e:
            errors.append(f"{bates_begin}: Vision OCR failed for {jpeg_path}: {e}")

    if vision_text_parts:
        text_content = "\n\n".join(vision_text_parts)

    native_storage_path = None
    if native_link:
        native_storage_path = f"{prefix}{native_link.replace(chr(92), '/')}"

    nl = record.get(FIELD_MAP_REVERSED.get("native_link", "Native Link"), "") or ""
    file_name = os.path.basename(nl.replace("\\", "/")) if nl else None
    doc = Document(
        production_id=production_id,
        bates_begin=bates_begin,
        bates_end=bates_end,
        page_count=page_count,
        metadata_={},
        text_content=text_content,
        native_path=native_storage_path,
        image_paths=jpeg_storage_paths,
        file_name=file_name,
    )
    _apply_metadata(doc, record, field_mapping)
    if native_storage_path and not doc.file_hash_sha256:
        try:
            import hashlib
            native_bytes = get_download_bytes(native_storage_path)
            doc.file_hash_sha256 = hashlib.sha256(native_bytes).hexdigest()
        except Exception as e:
            doc.extraction_status = "partial"
            doc.extraction_error = f"sha256 from native failed: {e}"
            errors.append(f"{bates_begin}: sha256 from native failed: {e}")
    return doc


async def _incr_skipped(db: AsyncSession, job_id: str) -> None:
    """Count one record as skipped."""
    await db.execute(
        text("UPDATE ingest_jobs SET skipped_files = skipped_files + 1 WHERE id = :jid"),
        {"jid": job_id},
    )
    await db.commit()


# Use cast(:errs as jsonb), NOT ":errs::jsonb" — SQLAlchemy's bind parser lets
# the ``::jsonb`` cast swallow the ``:errs`` parameter, so a literal ``:errs``
# reaches asyncpg and Postgres raises ``syntax error at or near ":"``.
_UPDATE_JOB_ERRORS_SQL = "UPDATE ingest_jobs SET errors = cast(:errs as jsonb) WHERE id = :jid"


async def _persist_job_errors(db: AsyncSession, job_id: str, errors: list[str]) -> None:
    """Persist the batch's collected error messages onto the job (JSONB column)."""
    await db.execute(
        text(_UPDATE_JOB_ERRORS_SQL),
        {"errs": json.dumps(errors), "jid": job_id},
    )
    await db.commit()


async def _persist_documents(db: AsyncSession, job_id: str, docs: list[Document]) -> None:
    """Persist a group of Documents atomically: one flush + one commit for all.

    Used for email families (parent + attachments), which must commit together —
    a failure before the single commit leaves nothing persisted, so a Cloud Tasks
    retry re-expands the whole container cleanly instead of skipping it on a
    partially-committed parent (whose native_path would satisfy the dedup check).
    """
    if not docs:
        return
    for doc in docs:
        db.add(doc)
    await db.flush()
    for doc in docs:
        await db.execute(
            text(
                "UPDATE documents SET text_search_vector = "
                "to_tsvector('english', COALESCE(text_content, '')), "
                "processing_status = 'complete' "
                "WHERE id = :id"
            ),
            {"id": doc.id},
        )
    await db.execute(
        text("UPDATE ingest_jobs SET processed_files = processed_files + :n WHERE id = :jid"),
        {"n": len(docs), "jid": job_id},
    )
    await db.commit()


async def _persist_document(db: AsyncSession, job_id: str, doc: Document) -> None:
    """Persist a single freshly built Document (flush, tsvector + status, progress)."""
    await _persist_documents(db, job_id, [doc])


async def _finalize_job_if_done(
    db: AsyncSession,
    job: "IngestJob",
    production_id: int,
    errors: list[str],
) -> None:
    """Finalize the job (AI titles + mark complete) once all files are accounted for."""
    from datetime import datetime, timezone

    await db.refresh(job)
    if (job.processed_files + job.skipped_files) >= job.total_files and job.status == "processing":
        result = await db.execute(
            select(Document).where(
                Document.production_id == production_id,
                Document.title.is_(None),
            )
        )
        untitled_docs = list(result.scalars().all())

        if settings.anthropic_api_key and untitled_docs:
            try:
                texts_for_titles = [(str(d.id), d.text_content) for d in untitled_docs]
                titles = await generate_titles_batch(texts_for_titles)
                for d in untitled_docs:
                    t = titles.get(str(d.id))
                    if t:
                        d.title = t
            except Exception as e:
                logger.exception("AI title generation failed")
                errors.append(f"AI title generation skipped: {e}")

        # Fallback: any doc still untitled (sparse/blank text, or no API key)
        # keeps its original filename so it is never left blank. Scoped to docs
        # that carry a "File Name" (the generic-PDF path); Relativity docs have
        # none and stay NULL exactly as before.
        for d in untitled_docs:
            if not d.title and isinstance(d.metadata_, dict) and d.metadata_.get("File Name"):
                d.title = os.path.splitext(d.metadata_["File Name"])[0][:200]
        await db.commit()

        job.status = "complete"
        job.errors = errors
        # Store naive UTC to match the tz-naive completed_at column (asyncpg
        # rejects an aware datetime against a TIMESTAMP WITHOUT TIME ZONE).
        job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()

        # Generate chunk embeddings for semantic search / clustering /
        # duplicate detection. Runs after the job is marked complete so a
        # long embedding pass (or a killed container) can never strand the
        # job in "processing". Best-effort — failures are logged only.
        try:
            from app.services.embeddings import embed_production_documents
            await embed_production_documents(db, production_id)
        except Exception:
            logger.exception("Embedding generation failed")

        # SP4b-2: derive email threads/inclusive for this production (best-effort;
        # a threading failure must never fail or un-complete the ingest job).
        try:
            from app.services.email_threading import derive_threads
            await derive_threads(db, production_id)
        except Exception:
            logger.exception("thread derivation skipped for production %s", production_id)

        # Ambient AI pipeline (clustering -> summaries -> brief). Best-effort:
        # never blocks ingest completion. Prod fans out via Cloud Tasks so the
        # long-running work doesn't ride on this request; locally we detach.
        try:
            from app.services import tasks as task_service
            from app.services.pipeline import run_ambient_pipeline

            if task_service.is_configured():
                task_service.enqueue_pipeline(production_id)
            else:
                task = asyncio.create_task(run_ambient_pipeline(production_id))
                _pipeline_tasks.add(task)
                task.add_done_callback(_pipeline_tasks.discard)
        except Exception:
            logger.exception("Failed to start ambient pipeline for production %s", production_id)


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

    from app.models import IngestJob

    job = await db.get(IngestJob, job_id)
    if not job:
        return

    field_mapping: dict | None = job.field_mapping or None

    records, opt_pages = bootstrap_ingest_source(
        production_id, (job.field_mapping or {}).get("load_prefix"))
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
                await _incr_skipped(db, job_id)
                continue
            if bates_begin in existing:
                await _incr_skipped(db, job_id)
                continue
            try:
                # Run the CPU/IO-bound conversion in a thread so it can't block
                # the event loop (a long render starves asyncpg and corrupts its
                # connection — "another operation is in progress").
                doc = await asyncio.to_thread(
                    process_ingest_record,
                    production_id, record, opt_pages, converted_tmp, errors,
                    field_mapping,
                )
                if doc is None:
                    await _incr_skipped(db, job_id)
                    continue
                _stamp_source(doc, job)
                await _persist_document(db, job_id, doc)
            except Exception as e:
                logger.exception("Failed to process record %s", bates_begin)
                errors.append(f"{bates_begin}: {e}")
                await db.rollback()
                await _incr_skipped(db, job_id)

        # Persist any error messages collected in this batch
        await _persist_job_errors(db, job_id, errors)

        await _finalize_job_if_done(db, job, production_id, errors)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def ingest_pdf_batch(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    start_idx: int,
    end_idx: int,
) -> None:
    """Process PDFs[start_idx:end_idx] for a generic-PDF ingest job.

    Idempotent: documents already present are skipped (matched by their
    source file path, native_path), so retried batches are safe — and it
    stays correct even if bates_begin is later edited (e.g. real Bates
    numbers backfilled over the synthetic control numbers).
    """
    from app.models import IngestJob, Production
    from app.services.ingest_pdf import (
        derive_bates_prefix,
        list_pdf_sources,
        process_pdf_record,
    )

    job = await db.get(IngestJob, job_id)
    if not job:
        return
    production = await db.get(Production, production_id)
    # Prefix is derived deterministically from the production name. All batch
    # workers for a job must see the same name — control numbers would diverge
    # if a production were renamed mid-ingest (not a supported workflow).
    prefix = derive_bates_prefix(production.name if production else "")

    fm = job.field_mapping or {}
    load_prefix = fm.get("load_prefix")
    offset = int(fm.get("control_offset") or 0)
    items = list_pdf_sources(production_id, load_prefix)
    errors: list[str] = list(job.errors or [])

    slice_pairs = [
        (idx, items[idx]) for idx in range(start_idx, min(end_idx, len(items)))
    ]
    # Skip PDFs already ingested, keyed on the stable source path (native_path)
    # rather than the now-mutable control number.
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
        control_number = f"{prefix} {offset + global_index + 1:06d}"
        if item["storage_path"] in existing:
            await _incr_skipped(db, job_id)
            continue
        try:
            # Offload rendering/OCR/upload to a thread; a large PDF rendered
            # inline would block the event loop and break the DB connection.
            doc = await asyncio.to_thread(
                process_pdf_record,
                production_id, item, offset + global_index, prefix, errors,
            )
            if doc is None:
                await _incr_skipped(db, job_id)
                continue
            _stamp_source(doc, job)
            await _persist_document(db, job_id, doc)
        except Exception as e:
            logger.exception("Failed to process PDF %s", item.get("relative_path"))
            errors.append(f"{control_number}: {e}")
            await db.rollback()
            await _incr_skipped(db, job_id)

    await _persist_job_errors(db, job_id, errors)

    await _finalize_job_if_done(db, job, production_id, errors)


async def run_ingest_batch(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    start_idx: int,
    end_idx: int,
) -> None:
    """Dispatch one batch to the right processor based on job.source_format."""
    from app.models import IngestJob

    job = await db.get(IngestJob, job_id)
    if job and job.source_format == "generic_pdf":
        await ingest_pdf_batch(db, job_id, production_id, start_idx, end_idx)
    elif job and job.source_format == "native":
        from app.services.ingest_native import ingest_native_batch
        await ingest_native_batch(db, job_id, production_id, start_idx, end_idx)
    else:
        await ingest_batch(db, job_id, production_id, start_idx, end_idx)


async def ingest_from_storage(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    production_name: str,
) -> None:
    """In-process fallback ingest used when Cloud Tasks isn't configured."""
    from datetime import datetime, timezone

    from app.models import IngestJob
    from app.services.ingest_pdf import list_pdf_sources
    from app.services.ingest_native import list_native_sources

    job = await db.get(IngestJob, job_id)
    if not job:
        return

    try:
        load_prefix = (job.field_mapping or {}).get("load_prefix")
        if job.source_format == "generic_pdf":
            total = len(list_pdf_sources(production_id, load_prefix))
            batch_step = 10
        elif job.source_format == "native":
            total = len(list_native_sources(production_id, load_prefix))
            batch_step = 10
        else:
            records, _ = bootstrap_ingest_source(production_id, load_prefix)
            total = len(records)
            batch_step = INGEST_BATCH_SIZE
        # Mirror the Cloud Tasks path's guard: with zero sources the batch
        # loop below never runs, so nothing would ever finalize the job and
        # it would sit in "processing" forever.
        if total == 0:
            raise FileNotFoundError("No ingestable files found in upload")
        job.total_files = total
        await db.commit()

        for start in range(0, total, batch_step):
            await run_ingest_batch(
                db, job_id, production_id, start, start + batch_step
            )
    except Exception as e:
        logger.exception("Inline ingest failed")
        job = await db.get(IngestJob, job_id)
        if job:
            job.status = "failed"
            job.errors = (job.errors or []) + [str(e)]
            # naive UTC to match the tz-naive completed_at column
            job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.commit()
