"""Generic PDF folder ingest — no Relativity load files required.

Each PDF becomes one Document: pages are rendered to JPEGs via PyMuPDF
and the embedded text layer is extracted, with a Cloud Vision OCR
fallback for scanned pages. Documents get a synthetic control number
in place of a Bates number.
"""

import logging
import re
from typing import Callable

import fitz  # PyMuPDF

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


def render_and_extract_pdf(
    pdf_bytes: bytes,
    ocr_fn: Callable[[bytes], str],
    dpi: int = RENDER_DPI,
) -> tuple[list[bytes], str, int]:
    """Render every page to a JPEG and extract its text.

    Returns (jpeg_bytes_per_page, combined_text, page_count). Uses the
    embedded text layer when present; calls ocr_fn(jpeg_bytes) for pages
    whose embedded text is empty/sparse (scanned pages).
    """
    jpeg_pages: list[bytes] = []
    text_parts: list[str] = []

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            jpeg = pix.tobytes("jpeg")
            jpeg_pages.append(jpeg)

            embedded = page.get_text().strip()
            if sum(1 for c in embedded if not c.isspace()) >= MIN_TEXT_CHARS:
                text_parts.append(embedded)
            else:
                ocr_text = ocr_fn(jpeg) or ""
                if ocr_text.strip():
                    text_parts.append(ocr_text.strip())

        page_count = doc.page_count
    finally:
        doc.close()

    return jpeg_pages, "\n\n".join(text_parts), page_count
