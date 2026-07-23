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
from app.services.email_parse import ParsedMessage, expand_email
from app.services.extractors import extract
from app.services.metadata_normalize import normalize_date
from app.services.storage import get_download_bytes, list_files

logger = logging.getLogger(__name__)

# Email containers handled by the one-file→many email path (SP4b-1; .mbox added
# in Task F2).
EMAIL_EXTS = {".eml", ".msg", ".pst", ".ost", ".mbox"}
# PST/OST/mbox containers explode into many messages whose raw bytes we do not
# keep, so their per-message hash comes from a deterministic re-serialization.
_PST_EXTS = {".pst", ".ost"}
_MBOX_EXTS = {".mbox"}
_TRANSIENT_CONTAINER_EXTS = _PST_EXTS | _MBOX_EXTS


def list_native_sources(production_id: int, load_prefix: str | None = None) -> list[dict]:
    """List ALL uploaded files for a production, sorted deterministically.

    Mirrors ``list_pdf_sources`` but is not filtered by extension.
    """
    prefix = f"productions/{production_id}/raw/{load_prefix or ''}"
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


def build_email_documents(
    parsed: ParsedMessage,
    message_control: str,
    production_id: int,
    source_path: str,
    custodian: str | None,
    msg_bytes: bytes,
    *,
    native_path: str | None = None,
    extract_fn=extract,
    ocr_fn=None,
) -> list[Document]:
    """Turn one parsed message into [parent, *attachment children].

    Pure: no DB, no storage. ``extract_fn``/``ocr_fn`` are injected so tests can
    avoid Vision OCR and real library calls. The parent's ``family_id`` is the
    message control number; every attachment shares it. The parent carries
    ``native_path`` (the container's storage path) so the batch dedup query can
    skip an already-ingested container on a Cloud Tasks retry; children leave it
    ``None`` (the parent's presence gates the whole container).
    """
    folder = os.path.dirname(source_path)
    parent_meta = {"File Name": os.path.basename(source_path) or message_control}
    if folder:
        parent_meta["Folder"] = folder

    parent = Document(
        production_id=production_id,
        bates_begin=message_control,
        bates_end=message_control,
        page_count=1,
        metadata_=parent_meta,
        title=(parsed.subject[:200] or None),
        text_content=parsed.body_text or None,
        native_path=native_path,
        image_paths=[],
        family_id=message_control,
        file_name=os.path.basename(source_path) or None,
        file_type="email",
        source_path=source_path,
        custodian=custodian,
        file_hash_sha256=hashlib.sha256(msg_bytes).hexdigest(),
        extraction_status="ok",
        email_from=(parsed.from_ or None),
        email_to=(parsed.to or None),
        email_cc=(parsed.cc or None),
        email_bcc=(parsed.bcc or None),
        email_subject=(parsed.subject or None),
        date_sent=normalize_date(parsed.date_sent) if parsed.date_sent else None,
        message_id=(parsed.message_id or None),
        in_reply_to=(parsed.in_reply_to or None),
        email_references=(parsed.references or None),
    )

    docs: list[Document] = [parent]
    for k, (att_name, att_bytes) in enumerate(parsed.attachments, start=1):
        att_control = f"{message_control} .{k:04d}"
        res = extract_fn(att_name, att_bytes, ocr_fn=ocr_fn)
        docs.append(
            Document(
                production_id=production_id,
                bates_begin=att_control,
                bates_end=att_control,
                page_count=1,
                metadata_={"File Name": att_name, "Parent": message_control},
                title=(att_name[:200] or None),
                text_content=res.text or None,
                native_path=None,
                image_paths=[],
                family_id=message_control,
                file_name=att_name,
                file_type=res.file_type,
                source_path=source_path,
                custodian=custodian,
                file_hash_sha256=hashlib.sha256(att_bytes).hexdigest(),
                extraction_status=res.extraction_status,
                extraction_error=res.extraction_error,
            )
        )
    return docs


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
        doc.extraction_status = "ok"
        return doc

    # Everything else: dispatch text extraction (images use Vision OCR).
    try:
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
    except Exception as e:
        errors.append(f"{control_number}: extraction failed: {e}")
        return None


def process_native_email(
    custodian: str | None,
    production_id: int,
    item: dict,
    global_index: int,
    prefix: str,
    errors: list[str],
) -> list[Document]:
    """Expand one email container into parent + attachment Documents. Never raises."""
    from app.services.ingest_pdf import _ocr_jpeg

    base_control = f"{prefix} {global_index + 1:06d}"
    storage_path = item["storage_path"]
    relative_path = item["relative_path"]
    filename = item["filename"]

    try:
        data = get_download_bytes(storage_path)
    except Exception as e:
        errors.append(f"{base_control}: could not download {relative_path}: {e}")
        return []

    messages = expand_email(filename, data)
    if not messages:
        errors.append(f"{base_control}: could not parse email container {relative_path}")
        return []

    multi = len(messages) > 1
    # A single .eml/.msg IS one message, so its file bytes are the message bytes.
    # PST/OST/mbox containers are exploded into transient messages we do not
    # keep (readpst's .eml files; mailbox's parsed records), so every message
    # hashes a deterministic re-serialization — regardless of how many messages
    # the container held (a 1-message container still isn't its own file's
    # bytes).
    is_transient_container = os.path.splitext(filename)[1].lower() in _TRANSIENT_CONTAINER_EXTS
    docs: list[Document] = []
    for m, parsed in enumerate(messages, start=1):
        message_control = f"{base_control} -{m:04d}" if multi else base_control
        msg_bytes = _serialize_message(parsed) if is_transient_container else data
        try:
            docs.extend(
                build_email_documents(
                    parsed,
                    message_control=message_control,
                    production_id=production_id,
                    source_path=relative_path,
                    custodian=custodian,
                    msg_bytes=msg_bytes,
                    native_path=storage_path,
                    ocr_fn=_ocr_jpeg,
                )
            )
        except Exception as e:
            errors.append(f"{message_control}: failed to build documents: {e}")
    return docs


def _serialize_message(parsed: ParsedMessage) -> bytes:
    """Deterministic byte serialization of a parsed message for hashing.

    PST/mbox messages are exploded to transient records we do not keep, so we
    hash a stable serialization of the parsed fields instead of the raw
    container.
    """
    header = "\n".join(
        [
            f"From: {parsed.from_}",
            f"To: {parsed.to}",
            f"Cc: {parsed.cc}",
            f"Bcc: {parsed.bcc}",
            f"Subject: {parsed.subject}",
            f"Date: {parsed.date_sent or ''}",
            "",
            parsed.body_text or "",
        ]
    )
    body = header.encode("utf-8", errors="replace")
    for name, blob in parsed.attachments:
        body += b"\n--att--" + name.encode("utf-8", errors="replace") + b"\n" + blob
    return body


async def ingest_native_batch(
    db: AsyncSession, job_id: str, production_id: int, start_idx: int, end_idx: int
) -> None:
    """Process one batch of native files. Mirrors ``ingest_pdf_batch``."""
    from app.services.ingest import (
        _finalize_job_if_done,
        _incr_skipped,
        _persist_document,
        _persist_documents,
        _persist_job_errors,
        _stamp_source,
    )
    from app.services.ingest_pdf import derive_bates_prefix

    job = await db.get(IngestJob, job_id)
    if not job:
        return
    production = await db.get(Production, production_id)
    prefix = derive_bates_prefix(production.name if production else "")
    fm = job.field_mapping or {}
    custodian = fm.get("custodian")
    load_prefix = fm.get("load_prefix")
    offset = int(fm.get("control_offset") or 0)

    items = list_native_sources(production_id, load_prefix)
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
        control_number = f"{prefix} {offset + global_index + 1:06d}"
        if item["storage_path"] in existing:
            await _incr_skipped(db, job_id)
            continue
        ext = os.path.splitext(item["filename"])[1].lower()
        try:
            if ext in EMAIL_EXTS:
                docs = await asyncio.to_thread(
                    process_native_email,
                    custodian, production_id, item, offset + global_index, prefix, errors,
                )
                if not docs:
                    await _incr_skipped(db, job_id)
                    continue
                # Commit the whole family in one transaction: if it fails
                # partway, nothing persists, so a retry re-expands the container
                # cleanly instead of finding a lone parent and skipping it.
                for d in docs:
                    _stamp_source(d, job)
                await _persist_documents(db, job_id, docs)
            else:
                doc = await asyncio.to_thread(
                    process_native_record,
                    custodian, production_id, item, offset + global_index, prefix, errors,
                )
                if doc is None:
                    await _incr_skipped(db, job_id)
                    continue
                _stamp_source(doc, job)
                await _persist_document(db, job_id, doc)
        except Exception as e:
            logger.exception("Failed to process native file %s", item.get("relative_path"))
            errors.append(f"{control_number}: {e}")
            await db.rollback()
            await _incr_skipped(db, job_id)

    await _persist_job_errors(db, job_id, errors)
    await _finalize_job_if_done(db, job, production_id, errors)
