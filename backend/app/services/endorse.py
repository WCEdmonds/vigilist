"""Pure endorsement drawing for produced documents (P2-2). No DB/network.

Stamps are black text on a white backing box so they stay legible on dark
scans. Fonts follow the codebase pattern: DejaVu with load_default fallback.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from app.services.production_numbering import format_bates

SLIP_W, SLIP_H = 1240, 1754  # A4 @ ~150 DPI, matches documents.py index pages
_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _load_font(size: int):
    try:
        return ImageFont.truetype(_DEJAVU_BOLD, size)
    except Exception:
        return ImageFont.load_default(size)


def page_bates_numbers(
    bates_begin: str, prefix: str, padding: int, page_count: int
) -> list[str]:
    """Every produced page carries its own sequential Bates number."""
    start = int(bates_begin[len(prefix):])
    return [format_bates(prefix, start + i, padding) for i in range(page_count)]


def _stamp_text(draw: ImageDraw.ImageDraw, img_w: int, img_h: int,
                text: str, font, corner: str) -> None:
    """corner: 'br' (Bates) or 'bl' (designation)."""
    pad = 8
    margin = max(10, int(img_h * 0.015))
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if corner == "br":
        x = img_w - margin - tw - pad * 2
    else:
        x = margin
    y = img_h - margin - th - pad * 2
    draw.rectangle([x, y, x + tw + pad * 2, y + th + pad * 2],
                   fill="white", outline="black")
    draw.text((x + pad - bbox[0], y + pad - bbox[1]), text, fill="black", font=font)


def stamp_page(img: Image.Image, bates_text: str,
               designation: str | None) -> Image.Image:
    """Return a stamped RGB copy: Bates bottom-right, designation bottom-left."""
    out = img.convert("RGB") if img.mode != "RGB" else img.copy()
    draw = ImageDraw.Draw(out)
    font = _load_font(max(14, out.height // 60))
    _stamp_text(draw, out.width, out.height, bates_text, font, "br")
    if designation:
        _stamp_text(draw, out.width, out.height, designation, font, "bl")
    return out


def slip_sheet(bates_text: str, designation: str | None,
               title: str = "DOCUMENT WITHHELD") -> Image.Image:
    """One white A4 page standing in for a withheld document."""
    page = Image.new("RGB", (SLIP_W, SLIP_H), "white")
    draw = ImageDraw.Draw(page)
    font = _load_font(48)
    bbox = draw.textbbox((0, 0), title, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((SLIP_W - tw) / 2 - bbox[0], (SLIP_H - th) / 2 - bbox[1]),
              title, fill="black", font=font)
    return stamp_page(page, bates_text, designation)
