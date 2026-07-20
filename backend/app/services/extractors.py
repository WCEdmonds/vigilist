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


_TEXT_EXTS = {".txt", ".csv", ".md", ".log", ".json", ".xml", ".html", ".htm", ".rtf"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp"}
# Extensions we deliberately do not handle here (email → SP4b; legacy binary Office).
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
                t = "".join(run.text for run in para.runs)
                if t.strip():
                    parts.append(t)
    return "\n".join(parts)


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
        if ext == ".pptx":
            t = _extract_pptx(data)
            return ExtractResult(t, "pptx", _status_for(t))
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
