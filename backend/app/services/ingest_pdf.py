"""Generic PDF folder ingest — no Relativity load files required.

Each PDF becomes one Document: pages are rendered to JPEGs via PyMuPDF
and the embedded text layer is extracted, with a Cloud Vision OCR
fallback for scanned pages. Documents get a synthetic control number
in place of a Bates number.
"""

import logging
import os
import re
from typing import Callable, Iterator

import fitz  # PyMuPDF

from app.models import Document
from app.services.storage import get_download_bytes, list_files, upload_bytes

logger = logging.getLogger(__name__)

RENDER_DPI = 250
# A page with fewer than this many non-whitespace characters of embedded
# text is treated as scanned and sent to OCR.
MIN_TEXT_CHARS = 10


def derive_bates_prefix(production_name: str) -> str:
    """Derive a Bates-style prefix from a production name.

    Uppercase, strip everything but A-Z/0-9/space, collapse whitespace,
    take the first token, truncate to 12 chars. Falls back to "DOC".
    """
    cleaned = re.sub(r"[^A-Z0-9 ]", "", (production_name or "").upper())
    tokens = cleaned.split()
    if not tokens:
        return "DOC"
    return tokens[0][:12]


# A filename stem that is just a control/Bates number (e.g. "SI001291",
# "ABC-000123", "0001234") carries no meaning as a title, so we let OCR-based
# smart renaming replace it. A stem with real words (spaces) is preserved.
_BATES_STUB_RE = re.compile(r"[A-Za-z]{0,8}[\s_.-]?\d{3,}[A-Za-z]?")


def looks_like_bates_stub(name: str) -> bool:
    """True if a filename stem looks like a Bates/control stub rather than a
    human-meaningful title (short alpha prefix + a run of digits, no words)."""
    return bool(_BATES_STUB_RE.fullmatch((name or "").strip()))


def iter_pdf_pages(
    pdf_bytes: bytes,
    ocr_fn: Callable[[bytes], str],
    dpi: int = RENDER_DPI,
) -> Iterator[tuple[int, bytes, str]]:
    """Yield (page_number, jpeg_bytes, page_text) one page at a time.

    Rendering one page at a time and letting the caller upload and drop each
    JPEG keeps peak memory bounded to a single page. Holding every rendered
    page in a list (the previous design) OOM-killed the worker on large PDFs.

    Uses the embedded text layer when present; calls ocr_fn(jpeg_bytes) for
    pages whose embedded text is empty/sparse (scanned pages).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_number, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            jpeg = pix.tobytes("jpeg")
            pix = None  # release the raw pixmap before OCR/yield

            embedded = page.get_text().strip()
            if sum(1 for c in embedded if not c.isspace()) >= MIN_TEXT_CHARS:
                page_text = embedded
            else:
                page_text = (ocr_fn(jpeg) or "").strip()

            yield page_number, jpeg, page_text
    finally:
        doc.close()


def list_pdf_sources(production_id: int, load_prefix: str | None = None) -> list[dict]:
    """List uploaded PDFs for a production, sorted deterministically.

    Returns a list of {storage_path, relative_path, filename} dicts.
    Slice indices into this list match across calls (sorted), so batch
    workers and retries process the same items.
    """
    prefix = f"productions/{production_id}/raw/{load_prefix or ''}"
    all_files = list_files(prefix)
    pdfs = [f for f in all_files if f.lower().endswith(".pdf")]
    pdfs.sort()

    items: list[dict] = []
    for path in pdfs:
        relative_path = path[len(prefix):] if path.startswith(prefix) else path
        items.append(
            {
                "storage_path": path,
                "relative_path": relative_path,
                "filename": os.path.basename(relative_path),
            }
        )
    return items


def _ocr_jpeg(jpeg_bytes: bytes) -> str:
    """OCR a single rendered page via Cloud Vision. Best-effort."""
    try:
        from app.services.ocr import ocr_image_vision_bytes

        return ocr_image_vision_bytes(jpeg_bytes)
    except Exception:
        logger.exception("Vision OCR failed for a rendered PDF page")
        return ""


def process_pdf_record(
    production_id: int,
    item: dict,
    global_index: int,
    prefix: str,
    errors: list[str],
) -> Document | None:
    """Turn one uploaded PDF into an unsaved Document.

    `global_index` is the file's 0-based position in the full sorted
    source list; the control number is derived from it so retried
    batches reproduce the same bates_begin.
    """
    control_number = f"{prefix} {global_index + 1:06d}"
    storage_path = item["storage_path"]
    relative_path = item["relative_path"]
    filename = item["filename"]

    try:
        pdf_bytes = get_download_bytes(storage_path)
    except Exception as e:
        errors.append(f"{control_number}: could not download {relative_path}: {e}")
        return None

    # Render + upload one page at a time so memory stays bounded to a single
    # page; only small strings (text + remote paths) accumulate across pages.
    image_paths: list[str] = []
    text_parts: list[str] = []
    page_count = 0
    stem = os.path.splitext(filename)[0]
    try:
        for page_num, jpeg, page_text in iter_pdf_pages(pdf_bytes, ocr_fn=_ocr_jpeg):
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
        errors.append(f"{control_number}: failed to render {relative_path}: {e}")
        return None

    text_content = "\n\n".join(text_parts)

    folder = os.path.dirname(relative_path)
    metadata = {"File Name": filename}
    if folder:
        metadata["Folder"] = folder

    # Meaningful filenames become the title directly; bare control/Bates-number
    # filenames are left untitled so the finalize pass can smart-rename them
    # from OCR text (with the filename kept as a fallback when no text exists).
    title = None if looks_like_bates_stub(stem) else stem[:200]

    return Document(
        production_id=production_id,
        bates_begin=control_number,
        bates_end=control_number,
        page_count=page_count or 1,
        metadata_=metadata,
        title=title,
        text_content=text_content or None,
        native_path=storage_path,
        image_paths=image_paths,
    )
