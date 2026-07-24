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
from app.services.ocr import PageOcr, sidecar_bytes, sidecar_remote_path
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


def _pdf_words_pct(page: "fitz.Page") -> list[dict]:
    """Word boxes from the embedded text layer, as percent-of-page coords.

    PyMuPDF reports PDF points relative to page.rect; the rendered JPEG has
    the same aspect, so percent coordinates transfer directly.

    For pages with a /Rotate entry, page.get_pixmap() renders the ROTATED
    view and page.rect reports the rotated dims, but page.get_text("words")
    coordinates are always in the UNROTATED frame. Each word rect is
    transformed through page.rotation_matrix (identity when unrotated) before
    percent conversion, so boxes land on the rotated content instead of the
    pre-rotation position.
    """
    rect = page.rect
    if not rect.width or not rect.height:
        return []
    matrix = page.rotation_matrix
    words: list[dict] = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        if not text.strip():
            continue
        r = fitz.Rect(x0, y0, x1, y1) * matrix
        r.normalize()
        xp0 = max(0.0, min(100.0, r.x0 / rect.width * 100))
        xp1 = max(0.0, min(100.0, r.x1 / rect.width * 100))
        yp0 = max(0.0, min(100.0, r.y0 / rect.height * 100))
        yp1 = max(0.0, min(100.0, r.y1 / rect.height * 100))
        # Endpoints-first rounding to preserve x+w <= 100 invariant
        x = round(xp0, 2)
        w = round(round(xp1, 2) - x, 2)
        y = round(yp0, 2)
        h = round(round(yp1, 2) - y, 2)
        if w <= 0 or h <= 0:
            continue
        words.append({
            "t": text,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "c": 1.0,
        })
    return words


def iter_pdf_pages(
    pdf_bytes: bytes,
    ocr_fn: Callable[[bytes], PageOcr | None],
    dpi: int = RENDER_DPI,
) -> Iterator[tuple[int, bytes, PageOcr | None]]:
    """Yield (page_number, jpeg_bytes, page_ocr) one page at a time.

    Rendering one page at a time and letting the caller upload and drop each
    JPEG keeps peak memory bounded to a single page. Holding every rendered
    page in a list (the previous design) OOM-killed the worker on large PDFs.

    Uses the embedded text layer (text + word boxes via PyMuPDF) when
    present; calls ocr_fn(jpeg_bytes) for pages whose embedded text is
    empty/sparse (scanned pages). page_ocr is None when ocr_fn failed.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_number, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            width, height = pix.width, pix.height
            jpeg = pix.tobytes("jpeg")
            pix = None  # release the raw pixmap before OCR/yield

            embedded = page.get_text().strip()
            if sum(1 for c in embedded if not c.isspace()) >= MIN_TEXT_CHARS:
                page_ocr: PageOcr | None = PageOcr(
                    text=embedded,
                    words=_pdf_words_pct(page),
                    width=width,
                    height=height,
                )
            else:
                page_ocr = ocr_fn(jpeg)

            yield page_number, jpeg, page_ocr
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


def _ocr_jpeg(jpeg_bytes: bytes) -> PageOcr | None:
    """OCR a single rendered page via Cloud Vision. None on failure."""
    try:
        from app.services.ocr import ocr_page_vision_bytes

        return ocr_page_vision_bytes(jpeg_bytes)
    except Exception:
        logger.exception("Vision OCR failed for a rendered PDF page")
        return None


def _ocr_jpeg_text(jpeg_bytes: bytes) -> str:
    """String-contract adapter for extractors.extract(ocr_fn=...).

    ``_ocr_jpeg`` returns ``PageOcr | None`` (the layout-aware contract used
    by ``iter_pdf_pages``); ``extract()`` expects a plain ``str`` and calls
    ``.strip()`` on the result, so the two contracts must never be mixed at
    that boundary.
    """
    page = _ocr_jpeg(jpeg_bytes)
    return page.text if page else ""


def upload_page_assets(
    production_id: int,
    control_number: str,
    page_num: int,
    jpeg: bytes,
    page_ocr: PageOcr | None,
    image_paths: list[str],
    ocr_paths: list[str],
    errors: list[str],
) -> None:
    """Upload page JPEG and (if present) sidecar, appending paths or errors.

    Computes page_stem from control_number and page_num. Uploads JPEG with
    content_type="image/jpeg"; on success appends remote path to image_paths,
    on failure appends "" and logs error. If page_ocr is None (OCR failed),
    appends "" to ocr_paths; otherwise uploads sidecar JSON with
    content_type="application/json". On sidecar success appends remote path,
    on failure appends "" and logs error. Semantics preserve the invariant
    that image_paths and ocr_paths are index-aligned and distinguishable
    (empty string for failure vs. empty words list in a successful PageOcr).
    """
    page_stem = f"{control_number.replace(' ', '_')}_{page_num:04d}"

    # Upload JPEG
    jpeg_remote = f"productions/{production_id}/converted/{page_stem}.jpg"
    try:
        upload_bytes(jpeg, jpeg_remote, content_type="image/jpeg")
        image_paths.append(jpeg_remote)
    except Exception as e:
        errors.append(f"{control_number}: image upload failed page {page_num}: {e}")
        image_paths.append("")

    # Upload sidecar (or mark failure)
    if page_ocr is None:
        ocr_paths.append("")  # OCR failed: distinguishable from words=[]
    else:
        try:
            sidecar_remote = sidecar_remote_path(production_id, page_stem)
            upload_bytes(
                sidecar_bytes(page_ocr),
                sidecar_remote,
                content_type="application/json",
            )
            ocr_paths.append(sidecar_remote)
        except Exception as e:
            errors.append(
                f"{control_number}: sidecar upload failed page {page_num}: {e}"
            )
            ocr_paths.append("")


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
    ocr_paths: list[str] = []
    text_parts: list[str] = []
    page_count = 0
    stem = os.path.splitext(filename)[0]
    try:
        for page_num, jpeg, page_ocr in iter_pdf_pages(pdf_bytes, ocr_fn=_ocr_jpeg):
            page_count = page_num
            if page_ocr is not None and page_ocr.text:
                text_parts.append(page_ocr.text)
            upload_page_assets(
                production_id, control_number, page_num, jpeg, page_ocr,
                image_paths, ocr_paths, errors
            )
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
        ocr_paths=ocr_paths,
    )
