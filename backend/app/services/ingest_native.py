"""Ingest a folder of loose native files (the `native` source_format).

Reuses the PDF page-render path for PDFs and the Python extraction dispatcher
for Office/text/image files. Per-file failures become error rows; the batch
never aborts on one bad file.
"""

import asyncio
import hashlib
import io
import logging
import os
import zipfile

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

# Zip containers handled by the explode-with-guards path (Task F3).
_ZIP_EXTS = {".zip"}
_ZIP_MAX_DEPTH = 2                            # zip-in-zip once; deeper zips are opaque leaves
_ZIP_MAX_ENTRIES = 500                        # accepted (leaf) entries per container, post-flatten
_ZIP_MAX_ENTRY_BYTES = 200 * 1024 * 1024      # per-entry uncompressed cap
_ZIP_MAX_TOTAL_BYTES = 1024 * 1024 * 1024     # aggregate uncompressed cap across the whole tree
_ZIP_ERROR_NOTE_CAP = 500                     # extraction_error is capped like every other note


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


def _expand_email_bytes(
    filename: str,
    data: bytes,
    *,
    base_control: str,
    production_id: int,
    source_path: str,
    custodian: str | None,
    errors: list[str],
    native_path: str | None = None,
    ocr_fn=None,
) -> list[Document]:
    """Parse an email container's bytes into parent+attachment Documents.

    Shared by ``process_native_email`` (top-level .eml/.msg/.pst/.ost/.mbox
    uploads) and the zip-intake path (Task F3) for a zip entry that is itself
    an email container — the SAME dispatch, no special-casing per entry type.
    """
    messages = expand_email(filename, data)
    if not messages:
        errors.append(f"{base_control}: could not parse email container {source_path}")
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
                    source_path=source_path,
                    custodian=custodian,
                    msg_bytes=msg_bytes,
                    native_path=native_path,
                    ocr_fn=ocr_fn,
                )
            )
        except Exception as e:
            errors.append(f"{message_control}: failed to build documents: {e}")
    return docs


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

    return _expand_email_bytes(
        filename,
        data,
        base_control=base_control,
        production_id=production_id,
        source_path=relative_path,
        custodian=custodian,
        errors=errors,
        native_path=storage_path,
        ocr_fn=_ocr_jpeg,
    )


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


def _zip_is_traversal(name: str) -> bool:
    return ".." in name.replace("\\", "/").split("/")


class _ZipExploder:
    """Recursively walk a zip's entries, enforcing the depth/count/size guards.

    Nested zips within ``_ZIP_MAX_DEPTH`` are transparently unwrapped so their
    leaf files land in the flat ``entries`` list — nested zips never become
    their own Document rows; only the outermost container does, and every
    unwrapped leaf joins ITS family (see ``build_zip_documents``). A zip found
    beyond the depth guard is kept as an opaque leaf entry (its bytes are
    appended as-is; downstream dispatch routes ``.zip`` through the generic
    extractor, which reports it "unsupported" — never explodes it further).

    Guards are checked with the entry's CLAIMED uncompressed size
    (``info.file_size``) before any bytes are read/decompressed, so a zip
    bomb (many entries each claiming close to the per-entry cap) can never
    push the running total past ``_ZIP_MAX_TOTAL_BYTES`` — the cap check
    always runs before the read that would grow the total.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, bytes]] = []
        self.skip_notes: list[str] = []
        self.total_bytes = 0
        self.truncated = False

    def explode(self, data: bytes, depth: int) -> None:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception as e:
            self.skip_notes.append(f"could not open nested zip at depth {depth}: {e}")
            return
        with zf:
            for info in zf.infolist():
                if self.truncated:
                    return
                if info.is_dir():
                    continue
                name = info.filename
                if _zip_is_traversal(name):
                    self.skip_notes.append(f"skipped {name}: path traversal")
                    continue
                if info.flag_bits & 0x1:
                    self.skip_notes.append(f"skipped {name}: encrypted")
                    continue
                if info.file_size > _ZIP_MAX_ENTRY_BYTES:
                    mb = _ZIP_MAX_ENTRY_BYTES // (1024 * 1024)
                    self.skip_notes.append(f"skipped {name}: exceeds {mb}MB uncompressed limit")
                    continue
                # Cap checks run BEFORE reading/decompressing this entry so a
                # zip-bomb-style container can never push total_bytes over the
                # aggregate cap, even transiently.
                if len(self.entries) >= _ZIP_MAX_ENTRIES:
                    self.truncated = True
                    self.skip_notes.append(
                        f"entry cap ({_ZIP_MAX_ENTRIES}) reached; remaining entries skipped"
                    )
                    return
                if self.total_bytes + info.file_size > _ZIP_MAX_TOTAL_BYTES:
                    self.truncated = True
                    self.skip_notes.append(
                        "total uncompressed size cap (1GB) reached; remaining entries skipped"
                    )
                    return
                try:
                    entry_bytes = zf.read(info)
                except Exception as e:
                    self.skip_notes.append(f"skipped {name}: could not read ({e})")
                    continue
                self.total_bytes += len(entry_bytes)
                ext = os.path.splitext(name)[1].lower()
                if ext in _ZIP_EXTS and depth < _ZIP_MAX_DEPTH:
                    self.explode(entry_bytes, depth + 1)
                else:
                    self.entries.append((name, entry_bytes))


def _build_zip_pdf_document(
    data: bytes,
    name: str,
    *,
    production_id: int,
    control_number: str,
    family_id: str,
    custodian: str | None,
    source_path: str,
    errors: list[str],
    ocr_fn,
) -> Document | None:
    """Render a PDF found inside a zip through the SAME page-render path as a
    top-level PDF upload (``process_pdf_record``), just fed from in-memory
    bytes instead of a storage download. Never raises."""
    from app.services.ingest_pdf import iter_pdf_pages, looks_like_bates_stub
    from app.services.storage import upload_bytes

    image_paths: list[str] = []
    text_parts: list[str] = []
    page_count = 0
    try:
        for page_num, jpeg, page_text in iter_pdf_pages(data, ocr_fn=ocr_fn):
            page_count = page_num
            if page_text:
                text_parts.append(page_text)
            remote = (
                f"productions/{production_id}/converted/"
                f"{control_number.replace(' ', '_')}_{page_num:04d}.jpg"
            )
            try:
                upload_bytes(jpeg, remote, content_type="image/jpeg")
                image_paths.append(remote)
            except Exception as e:
                errors.append(f"{control_number}: image upload failed page {page_num}: {e}")
                image_paths.append("")
    except Exception as e:
        errors.append(f"{control_number}: failed to render {name}: {e}")
        return None

    stem = os.path.splitext(name)[0]
    title = None if looks_like_bates_stub(stem) else stem[:200]
    return Document(
        production_id=production_id,
        bates_begin=control_number,
        bates_end=control_number,
        page_count=page_count or 1,
        metadata_={"File Name": name, "Parent": family_id},
        title=title,
        text_content=("\n\n".join(text_parts) or None),
        native_path=None,
        image_paths=image_paths,
        family_id=family_id,
        file_name=name,
        file_type="pdf",
        source_path=source_path,
        custodian=custodian,
        file_hash_sha256=hashlib.sha256(data).hexdigest(),
        extraction_status="ok",
    )


def _build_zip_child_documents(
    name: str,
    data: bytes,
    *,
    production_id: int,
    custodian: str | None,
    source_path: str,
    control_number: str,
    family_id: str,
    errors: list[str],
    ocr_fn=None,
) -> list[Document]:
    """Dispatch one accepted zip entry through the NORMAL per-file pipeline —
    PDFs render like top-level PDFs, email containers expand like top-level
    email containers, everything else routes through the shared extractor.
    No special-casing per entry type; every doc produced joins ``family_id``
    (the OUTERMOST zip container's family, per the email-attachment
    convention). Never raises — worst case is a skipped/error entry."""
    from app.services.ingest_pdf import _ocr_jpeg, looks_like_bates_stub

    effective_ocr = ocr_fn if ocr_fn is not None else _ocr_jpeg
    ext = os.path.splitext(name)[1].lower()

    if ext == ".pdf":
        doc = _build_zip_pdf_document(
            data,
            name,
            production_id=production_id,
            control_number=control_number,
            family_id=family_id,
            custodian=custodian,
            source_path=source_path,
            errors=errors,
            ocr_fn=effective_ocr,
        )
        return [doc] if doc else []

    if ext in EMAIL_EXTS:
        docs = _expand_email_bytes(
            name,
            data,
            base_control=control_number,
            production_id=production_id,
            source_path=source_path,
            custodian=custodian,
            errors=errors,
            native_path=None,
            ocr_fn=effective_ocr,
        )
        # Every message/attachment this email container expands into still
        # joins the zip container's family — not its own per-message family.
        for d in docs:
            d.family_id = family_id
        return docs

    try:
        res = extract(name, data, ocr_fn=effective_ocr)
    except Exception as e:  # extract() already never raises; belt and suspenders
        errors.append(f"{control_number}: extraction failed: {e}")
        return []

    stem = os.path.splitext(name)[0]
    title = None if looks_like_bates_stub(stem) else stem[:200]
    return [
        Document(
            production_id=production_id,
            bates_begin=control_number,
            bates_end=control_number,
            page_count=1,
            metadata_={"File Name": name, "Parent": family_id},
            title=title,
            text_content=res.text or None,
            native_path=None,
            image_paths=[],
            family_id=family_id,
            file_name=name,
            file_type=res.file_type,
            source_path=source_path,
            custodian=custodian,
            file_hash_sha256=hashlib.sha256(data).hexdigest(),
            extraction_status=res.extraction_status,
            extraction_error=res.extraction_error,
        )
    ]


def build_zip_documents(
    data: bytes,
    *,
    container_control: str,
    production_id: int,
    source_path: str,
    custodian: str | None,
    native_path: str | None = None,
    ocr_fn=None,
) -> list[Document]:
    """Turn one zip container's bytes into [container, *children]. Never raises.

    Mirrors ``build_email_documents``: the container is the family root
    (``family_id`` = its own control number) and every accepted entry —
    dispatched through the normal per-file pipeline — shares that family_id,
    including entries recovered from nested zips within the depth guard.
    ``extraction_status`` is ``"ok"`` only when nothing was skipped; any skip
    (encrypted/traversal/oversize/guard-cap) downgrades it to ``"partial"``
    with a joined, 500-char-capped note in ``extraction_error``. A container
    that cannot even be opened as a zip becomes a single ``"error"`` row.
    """
    folder = os.path.dirname(source_path)
    parent_meta = {"File Name": os.path.basename(source_path) or container_control}
    if folder:
        parent_meta["Folder"] = folder

    family_id = container_control
    container = Document(
        production_id=production_id,
        bates_begin=container_control,
        bates_end=container_control,
        page_count=1,
        metadata_=parent_meta,
        title=(os.path.splitext(os.path.basename(source_path))[0][:200] or None),
        text_content=None,
        native_path=native_path,
        image_paths=[],
        family_id=family_id,
        file_name=os.path.basename(source_path) or None,
        file_type="zip",
        source_path=source_path,
        custodian=custodian,
        file_hash_sha256=hashlib.sha256(data).hexdigest(),
        extraction_status="ok",
    )

    try:
        with zipfile.ZipFile(io.BytesIO(data)):
            pass
    except Exception as e:
        container.extraction_status = "error"
        container.extraction_error = f"could not open zip: {e}"[:_ZIP_ERROR_NOTE_CAP]
        return [container]

    exploder = _ZipExploder()
    try:
        exploder.explode(data, depth=1)
    except Exception as e:
        # Guards above are exhaustive, but a corrupt member deep in the tree
        # could still raise something zipfile-specific we didn't anticipate —
        # keep whatever children were already found and surface the rest as a
        # skip note rather than losing the whole container.
        exploder.skip_notes.append(f"explode aborted: {e}")
        exploder.truncated = True

    docs: list[Document] = [container]
    errors: list[str] = []
    for k, (name, entry_bytes) in enumerate(exploder.entries, start=1):
        entry_control = f"{container_control} .{k:04d}"
        try:
            docs.extend(
                _build_zip_child_documents(
                    name,
                    entry_bytes,
                    production_id=production_id,
                    custodian=custodian,
                    source_path=source_path,
                    control_number=entry_control,
                    family_id=family_id,
                    errors=errors,
                    ocr_fn=ocr_fn,
                )
            )
        except Exception as e:
            errors.append(f"{entry_control}: failed to build documents: {e}")

    notes = exploder.skip_notes + errors
    if notes:
        container.extraction_status = "partial"
        container.extraction_error = "; ".join(notes)[:_ZIP_ERROR_NOTE_CAP]

    return docs


def process_native_zip(
    custodian: str | None,
    production_id: int,
    item: dict,
    global_index: int,
    prefix: str,
    errors: list[str],
) -> list[Document]:
    """Explode one .zip container into container + child Documents. Never raises."""
    from app.services.ingest_pdf import _ocr_jpeg

    control_number = f"{prefix} {global_index + 1:06d}"
    storage_path = item["storage_path"]
    relative_path = item["relative_path"]

    try:
        data = get_download_bytes(storage_path)
    except Exception as e:
        errors.append(f"{control_number}: could not download {relative_path}: {e}")
        return []

    try:
        return build_zip_documents(
            data,
            container_control=control_number,
            production_id=production_id,
            source_path=relative_path,
            custodian=custodian,
            native_path=storage_path,
            ocr_fn=_ocr_jpeg,
        )
    except Exception as e:
        logger.exception("Zip explode failed for %s", relative_path)
        errors.append(f"{control_number}: failed to explode zip container {relative_path}: {e}")
        return [
            Document(
                production_id=production_id,
                bates_begin=control_number,
                bates_end=control_number,
                page_count=1,
                metadata_={"File Name": os.path.basename(relative_path) or control_number},
                title=None,
                text_content=None,
                native_path=storage_path,
                image_paths=[],
                family_id=control_number,
                file_name=os.path.basename(relative_path) or None,
                file_type="zip",
                source_path=relative_path,
                custodian=custodian,
                file_hash_sha256=hashlib.sha256(data).hexdigest(),
                extraction_status="error",
                extraction_error=str(e)[:_ZIP_ERROR_NOTE_CAP],
            )
        ]


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
            elif ext in _ZIP_EXTS:
                docs = await asyncio.to_thread(
                    process_native_zip,
                    custodian, production_id, item, offset + global_index, prefix, errors,
                )
                if not docs:
                    await _incr_skipped(db, job_id)
                    continue
                # Same all-or-nothing family commit as the email path: a
                # retry re-explodes the container cleanly instead of finding
                # a lone container row and skipping it.
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
