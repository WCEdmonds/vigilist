"""Burn redaction rectangles into page images (P1-2/3).

Pure pixel work: no DB, no network, no storage. The as-produced rendition
endpoints and (later) the Phase-2 production pipeline both call burn_page.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from PIL import Image, ImageDraw, ImageFont


class RectLike(Protocol):
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    reason_code: str


# White stamp shown inside the black box when it fits. Privilege logs
# cross-reference these labels, so keep them stable.
REASON_LABELS: dict[str, str] = {
    "attorney_client": "ATTORNEY-CLIENT",
    "work_product": "WORK PRODUCT",
    "pii": "PII",
    "phi": "PHI",
    "confidential": "CONFIDENTIAL",
    "trade_secret": "TRADE SECRET",
    "non_responsive": "NON-RESPONSIVE",
    "other": "REDACTED",
}

_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(_DEJAVU_BOLD, size)
    except Exception:
        return ImageFont.load_default(size)  # Pillow >= 10.1 scalable fallback


def burn_page(img: Image.Image, rects: Sequence[RectLike]) -> Image.Image:
    """Return a copy of img with each rect burned in as an opaque black box.

    Coordinates are 0-100 percentages of the page. The reason-code label is
    stamped in white, centered, only when it fits inside the box.
    """
    out = img.copy()
    if out.mode != "RGB":
        out = out.convert("RGB")
    if not rects:
        return out
    draw = ImageDraw.Draw(out)
    for r in rects:
        x0 = min(out.width, max(0.0, r.x_pct / 100.0 * out.width))
        y0 = min(out.height, max(0.0, r.y_pct / 100.0 * out.height))
        x1 = min(out.width, max(x0, (r.x_pct + r.w_pct) / 100.0 * out.width))
        y1 = min(out.height, max(y0, (r.y_pct + r.h_pct) / 100.0 * out.height))
        if x1 <= x0 or y1 <= y0:
            continue
        draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0))

        label = REASON_LABELS.get(r.reason_code, "REDACTED")
        box_w, box_h = x1 - x0, y1 - y0
        size = max(12, int(box_h * 0.35))
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # Shrink until the label fits comfortably inside the box (or give up).
        while (tw > box_w * 0.9 or th > box_h * 0.8) and size > 12:
            size = max(12, size - 4)
            font = _load_font(size)
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= box_w * 0.9 and th <= box_h * 0.8:
            pos = (x0 + (box_w - tw) / 2 - bbox[0], y0 + (box_h - th) / 2 - bbox[1])
            draw.text(pos, label, fill=(255, 255, 255), font=font)
    return out
