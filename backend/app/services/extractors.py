"""Extract text from loose native files using Python libraries.

Pure and deterministic: no DB, no storage. Vision OCR for images is injected
via ``ocr_fn`` so callers/tests control it. Extraction never raises — parse
failures become an ``error`` result so the ingest batch can continue.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass


@dataclass
class ExtractResult:
    text: str
    file_type: str
    extraction_status: str            # ok | partial | unsupported | error
    extraction_error: str | None = None


_TEXT_EXTS = {".txt", ".csv", ".md", ".log", ".json", ".xml", ".html", ".htm"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp"}
# Extensions we deliberately do not handle here (email → SP4b; legacy binary Office).
# Documented only — the routing catch-all handles these via fall-through.
_UNSUPPORTED_EXTS = {".doc", ".xls", ".ppt", ".msg", ".eml", ".pst"}


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _status_for(text: str) -> str:
    return "ok" if text.strip() else "partial"


def _extract_docx(data: bytes) -> str:
    from docx import Document as Docx
    doc = Docx(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text)
    return "\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append("\t".join(cells))
        return "\n".join(parts)
    finally:
        wb.close()


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    parts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                t = para.text
                if t.strip():
                    parts.append(t)
    return "\n".join(parts)


_ODT_XML_CAP = 50 * 1024 * 1024  # content.xml decompressed cap — bomb guard


def _read_zip_member_bounded(zf, name: str, limit: int) -> bytes:
    """Read a zip member with a hard output bound, enforced against actual
    decompressed bytes (never the entry's claimed/uncompressed size, which is
    attacker-controlled). Raises ValueError the moment the true decompressed
    size would exceed ``limit`` — never buffers the full bomb in memory."""
    chunks: list[bytes] = []
    read = 0
    with zf.open(name) as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            read += len(chunk)
            if read > limit:
                raise ValueError(f"{name} exceeds decompressed size limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _extract_odt(data: bytes) -> str:
    import re
    import zipfile as _zipfile
    with _zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = _read_zip_member_bounded(zf, "content.xml", _ODT_XML_CAP).decode("utf-8", errors="replace")
    # <text:p>/<text:h> delimit paragraphs; strip all other tags.
    xml = re.sub(r"</text:(p|h)>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _extract_rtf(data: bytes) -> str:
    from striprtf.striprtf import rtf_to_text
    return rtf_to_text(data.decode("latin-1", errors="replace"), errors="ignore")


def _extract_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def extract(filename: str, data: bytes, ocr_fn=None) -> ExtractResult:
    """Route ``data`` to the extractor for ``filename``'s extension.

    NOTE: ``.pdf`` is intentionally NOT handled here — callers delegate PDFs to
    ``process_pdf_record`` (page render + OCR). A ``.pdf`` reaching this function
    is treated as unsupported.
    """
    ext = _ext(filename)
    ft = ext.lstrip(".") or "unknown"
    try:
        if ext == ".docx":
            t = _extract_docx(data)
            return ExtractResult(t, "docx", _status_for(t))
        if ext == ".xlsx":
            t = _extract_xlsx(data)
            return ExtractResult(t, "xlsx", _status_for(t))
        if ext in (".pptx", ".potx"):
            t = _extract_pptx(data)
            return ExtractResult(t, ext.lstrip("."), _status_for(t))
        if ext == ".odt":
            t = _extract_odt(data)
            return ExtractResult(t, "odt", _status_for(t))
        if ext == ".rtf":
            t = _extract_rtf(data)
            return ExtractResult(t, "rtf", _status_for(t))
        if ext in _TEXT_EXTS:
            t = _extract_text(data)
            return ExtractResult(t, ft, _status_for(t))
        if ext in _IMAGE_EXTS:
            t = (ocr_fn(data) if ocr_fn else "") or ""
            return ExtractResult(t, "image", _status_for(t))
        # Legacy Office, email, unknown/no extension.
        return ExtractResult("", ft, "unsupported")
    except Exception as e:  # never raise — a bad file is an error row, not a crash
        return ExtractResult("", ft, "error", str(e)[:500])


TINY_IMAGE_BYTES = 20_000
TINY_IMAGE_SIDE_PX = 150


def should_ocr_image(name: str, data: bytes, inline: bool) -> bool:
    """False only for tiny images (logos, signature icons, tracking pixels):
    under TINY_IMAGE_BYTES, or a short side under TINY_IMAGE_SIDE_PX. An image
    PIL cannot read is OCR'd, since we cannot prove it is junk.

    ``inline`` (cid:-referenced) no longer affects the decision: real photos
    pasted into an email body are inline too and must be OCR'd. The parameter
    stays so callers need not change."""
    if len(data) < TINY_IMAGE_BYTES:
        return False
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return min(im.size) >= TINY_IMAGE_SIDE_PX
    except Exception:
        return True


# Pages OCR'd per PDF attachment. A scanned attachment of thousands of pages
# would otherwise be billed (and rendered) page by page inside one worker
# request; pages past the cap keep their text layer only.
MAX_OCR_PAGES_PER_ATTACHMENT = 50


def extract_pdf_text(data: bytes, ocr_fn) -> ExtractResult:
    """Text for a PDF attachment: PyMuPDF text layer per page, OCR only for
    pages under the ``ingest_pdf`` sparse-text gate, at most
    ``MAX_OCR_PAGES_PER_ATTACHMENT`` of them. ``ocr_fn(jpeg) -> str``."""
    import fitz

    from app.services.ingest_pdf import MIN_TEXT_CHARS, RENDER_DPI

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return ExtractResult("", "pdf", "error", str(e)[:500])
    parts: list[str] = []
    page_errors: list[str] = []
    ocr_pages = 0
    over_cap = 0
    try:
        for page in doc:
            embedded = page.get_text().strip()
            if sum(1 for c in embedded if not c.isspace()) >= MIN_TEXT_CHARS:
                parts.append(embedded)
            elif ocr_fn is not None and ocr_pages >= MAX_OCR_PAGES_PER_ATTACHMENT:
                over_cap += 1
                if embedded:
                    parts.append(embedded)
            elif ocr_fn is not None:
                ocr_pages += 1
                try:
                    jpeg = page.get_pixmap(dpi=RENDER_DPI, alpha=False).tobytes("jpeg")
                    t = (ocr_fn(jpeg) or "").strip()
                except Exception as e:  # one bad page must not drop the rest
                    page_errors.append(f"page {page.number + 1}: OCR failed: {str(e)[:100]}")
                    continue
                if t:
                    parts.append(t)
    except Exception as e:
        text = "\n\n".join(parts)
        return ExtractResult(text, "pdf", "error", str(e)[:500])
    finally:
        doc.close()
    text = "\n\n".join(parts)
    if over_cap:
        page_errors.insert(0, f"{over_cap} pages past the {MAX_OCR_PAGES_PER_ATTACHMENT}-page OCR cap "
                              "were not OCR'd (text layer only)")
    if page_errors:
        return ExtractResult(text, "pdf", "partial", "; ".join(page_errors)[:500])
    return ExtractResult(text, "pdf", _status_for(text))


def _extract_eml(data: bytes) -> ExtractResult:
    """Text of an attached message (e.g. an embedded PST message): its
    headers, body and attachment names."""
    try:
        from app.services.email_parse import _parse_eml_bytes

        p = _parse_eml_bytes(data)
        lines = [f"{label}: {value}" for label, value in (
            ("From", p.from_), ("To", p.to), ("Cc", p.cc), ("Date", p.date_sent or ""),
            ("Subject", p.subject)) if value]
        if p.body_text:
            lines += ["", p.body_text]
        names = [n for n, _ in p.attachments]
        if names:
            lines += ["", "Attachments: " + "; ".join(names)]
        text = "\n".join(lines)
        return ExtractResult(text, "email", _status_for(text))
    except Exception as e:
        return ExtractResult("", "email", "error", str(e)[:500])


def extract_attachment(name: str, data: bytes, *, inline: bool, ocr_fn) -> ExtractResult:
    """Extraction for an email attachment: PDFs get text, tiny images
    skip OCR (empty text, status ok), everything else uses ``extract``."""
    ext = _ext(name)
    if ext == ".pdf":
        return extract_pdf_text(data, ocr_fn)
    if ext == ".eml":
        return _extract_eml(data)
    if ext in _IMAGE_EXTS and not should_ocr_image(name, data, inline):
        return ExtractResult("", "image", "ok")
    return extract(name, data, ocr_fn=ocr_fn)
