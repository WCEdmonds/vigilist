"""Generic PDF folder ingest — no Relativity load files required.

Each PDF becomes one Document: pages are rendered to JPEGs via PyMuPDF
and the embedded text layer is extracted, with a Cloud Vision OCR
fallback for scanned pages. Documents get a synthetic control number
in place of a Bates number.
"""

import logging
import re

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
