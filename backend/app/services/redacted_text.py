"""Produced text for redacted documents: the page text minus the words under
redaction boxes.

Built from each page's word sidecar (ocr_paths), whose boxes share the
redactions' 0-100 page coordinates. A word goes when at least 30 % of it
lies under a box (erring toward removal); each run of removed words becomes
one [REDACTED]. If any page lacks a readable sidecar there is no safe way to
know what the boxes cover, so no text ships at all — never text_content,
which has no positions.
"""

from __future__ import annotations

import json
import logging
from statistics import median

from app.services import storage

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
REDACTED_PAGE = "[REDACTED PAGE]"
WORD_COVER_THRESHOLD = 0.30


def covered_fraction(word: dict, rect) -> float:
    area = word["w"] * word["h"]
    if area <= 0:
        return 0.0
    w = min(word["x"] + word["w"], rect.x_pct + rect.w_pct) - max(word["x"], rect.x_pct)
    h = min(word["y"] + word["h"], rect.y_pct + rect.h_pct) - max(word["y"], rect.y_pct)
    return round(max(0.0, w) * max(0.0, h) / area, 6)


def _whole_page(rect) -> bool:
    return (rect.x_pct <= 0.5 and rect.y_pct <= 0.5
            and rect.x_pct + rect.w_pct >= 99.5 and rect.y_pct + rect.h_pct >= 99.5)


def _lines(words: list[dict]) -> list[list[dict]]:
    if not words:
        return []
    tol = median(w["h"] for w in words) / 2
    lines: list[list[dict]] = []
    centre = None
    for w in sorted(words, key=lambda w: (w["y"] + w["h"] / 2, w["x"])):
        c = w["y"] + w["h"] / 2
        if lines and abs(c - centre) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
            centre = c
    return [sorted(line, key=lambda w: w["x"]) for line in lines]


def page_text(words: list[dict], rects: list) -> str:
    if any(_whole_page(r) for r in rects):
        return REDACTED_PAGE
    out_lines = []
    for line in _lines(words):
        tokens: list[str] = []
        for w in line:
            hidden = any(covered_fraction(w, r) >= WORD_COVER_THRESHOLD for r in rects)
            if hidden:
                if not tokens or tokens[-1] != REDACTED:
                    tokens.append(REDACTED)
            else:
                tokens.append(w["t"])
        out_lines.append(" ".join(tokens))
    return "\n".join(out_lines)


def produced_text(sidecars: list[dict | None], redactions_by_page: dict[int, list]) -> str | None:
    """sidecars[i] is page i+1's sidecar JSON. None if any page is missing."""
    if not sidecars or any(s is None for s in sidecars):
        return None
    return "\f".join(
        page_text(s.get("words") or [], redactions_by_page.get(n, []))
        for n, s in enumerate(sidecars, start=1)
    )


def load_sidecars(doc) -> list[dict | None]:
    """One entry per page image; None where the sidecar is absent or unreadable."""
    images = getattr(doc, "image_paths", None) or []
    paths = list(getattr(doc, "ocr_paths", None) or [])
    out: list[dict | None] = []
    for i in range(len(images)):
        path = paths[i] if i < len(paths) else ""
        if not path:
            out.append(None)
            continue
        try:
            out.append(json.loads(storage.get_download_bytes(path)))
        except Exception:
            logger.warning("Unreadable sidecar %s", path)
            out.append(None)
    return out
