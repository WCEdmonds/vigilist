"""OCR service using Google Cloud Vision API, with Tesseract fallback."""

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Vision TextAnnotation.DetectedBreak.BreakType enum values, defined locally
# so this module imports without google-cloud-vision installed.
_BREAK_SPACE = 1
_BREAK_SURE_SPACE = 2
_BREAK_EOL_SURE_SPACE = 3
_BREAK_HYPHEN = 4
_BREAK_LINE_BREAK = 5

_SEPARATORS = {
    _BREAK_SPACE: " ",
    _BREAK_SURE_SPACE: " ",
    _BREAK_EOL_SURE_SPACE: "\n",
    _BREAK_LINE_BREAK: "\n",
    _BREAK_HYPHEN: "-",  # line-wrap hyphen: keep it, no space or break
}


@dataclass
class PageOcr:
    """One page of OCR output: layout-aware text + percent-coord word boxes."""

    text: str = ""
    words: list = field(default_factory=list)
    width: int = 0
    height: int = 0


def _detected_break(word) -> int:
    symbols = list(getattr(word, "symbols", None) or [])
    if not symbols:
        return 0
    prop = getattr(symbols[-1], "property", None)
    brk = getattr(prop, "detected_break", None) if prop else None
    if brk is None:
        return 0
    raw = getattr(brk, "type_", None)
    if raw is None:
        raw = getattr(brk, "type", 0)
    return int(raw or 0)


def _word_box_pct(word, width: int, height: int) -> dict | None:
    """Axis-aligned envelope of the word's vertices as percent-of-page.

    Returns None (word omitted from boxes, kept in text) when the page has
    no dimensions or the clamped box is degenerate.
    """
    if not width or not height:
        return None
    vertices = list(getattr(getattr(word, "bounding_box", None), "vertices", None) or [])
    if not vertices:
        return None
    xs = [(getattr(v, "x", 0) or 0) for v in vertices]
    ys = [(getattr(v, "y", 0) or 0) for v in vertices]
    x0 = max(0.0, min(100.0, min(xs) / width * 100))
    x1 = max(0.0, min(100.0, max(xs) / width * 100))
    y0 = max(0.0, min(100.0, min(ys) / height * 100))
    y1 = max(0.0, min(100.0, max(ys) / height * 100))

    # Round endpoints first, then compute width/height from rounded endpoints
    # to ensure x + w <= 100 and y + h <= 100 (no independent rounding).
    x_rounded = round(x0, 2)
    x1_rounded = round(x1, 2)
    y_rounded = round(y0, 2)
    y1_rounded = round(y1, 2)

    w = round(x1_rounded - x_rounded, 2)
    h = round(y1_rounded - y_rounded, 2)

    # Drop degenerate boxes after rounding (collapse to zero width/height).
    if w <= 0 or h <= 0:
        return None

    return {
        "t": "".join(s.text for s in word.symbols),
        "x": x_rounded,
        "y": y_rounded,
        "w": w,
        "h": h,
        "c": round((getattr(word, "confidence", 0.0) or 0.0), 2),
    }


def page_ocr_from_annotation(fta) -> PageOcr:
    """Build a PageOcr from a Vision full_text_annotation-shaped object.

    Pure attribute-access parsing (works on protobufs and test doubles).
    Falls back to the flat .text when the annotation has no page structure.
    """
    pages = list(getattr(fta, "pages", None) or []) if fta else []
    if not pages:
        flat = (getattr(fta, "text", "") or "").strip() if fta else ""
        return PageOcr(text=flat)

    words: list[dict] = []
    page_texts: list[str] = []
    for page in pages:
        pw = getattr(page, "width", 0) or 0
        ph = getattr(page, "height", 0) or 0
        block_texts: list[str] = []
        for block in getattr(page, "blocks", None) or []:
            para_texts: list[str] = []
            for para in getattr(block, "paragraphs", None) or []:
                parts: list[str] = []
                for word in getattr(para, "words", None) or []:
                    box = _word_box_pct(word, pw, ph)
                    if box is not None:
                        words.append(box)
                    parts.append("".join(s.text for s in word.symbols))
                    parts.append(_SEPARATORS.get(_detected_break(word), " "))
                para_text = "".join(parts).rstrip()
                if para_text:
                    para_texts.append(para_text)
            if para_texts:
                block_texts.append("\n".join(para_texts))
        if block_texts:
            page_texts.append("\n\n".join(block_texts))

    first = pages[0]
    return PageOcr(
        text="\n\n".join(page_texts).strip(),
        words=words,
        width=getattr(first, "width", 0) or 0,
        height=getattr(first, "height", 0) or 0,
    )


def sidecar_bytes(page: PageOcr) -> bytes:
    """Serialize one page's boxes as the v1 sidecar JSON."""
    payload = {"v": 1, "width": page.width, "height": page.height, "words": page.words}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sidecar_remote_path(production_id: int, stem: str) -> str:
    """Sidecar object path; stem must match the page JPEG's filename stem."""
    return f"productions/{production_id}/ocr/{stem}.json"


def ocr_page_vision_bytes(image_bytes: bytes) -> PageOcr:
    """OCR one page image via Cloud Vision, keeping layout + word boxes."""
    from google.cloud import vision

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")

    return page_ocr_from_annotation(response.full_text_annotation)


def ocr_image_vision(image_path: str) -> str:
    """Extract text from an image using Google Cloud Vision API."""
    from google.cloud import vision

    client = vision.ImageAnnotatorClient()

    with open(image_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")

    text = response.full_text_annotation.text if response.full_text_annotation else ""
    return text.strip()


def ocr_image_vision_bytes(image_bytes: bytes) -> str:
    """Extract text from image bytes using Google Cloud Vision API."""
    return ocr_page_vision_bytes(image_bytes).text


def ocr_image_tesseract(image_path: str) -> str:
    """Fallback: extract text from an image using Tesseract."""
    import pytesseract
    from PIL import Image

    img = Image.open(image_path)
    text = pytesseract.image_to_string(img)
    return text.strip() if text else ""


def ocr_image(image_path: str, use_vision: bool = True) -> str:
    """Extract text from an image. Uses Cloud Vision if available, falls back to Tesseract."""
    if use_vision:
        try:
            text = ocr_image_vision(image_path)
            if text:
                return text
        except Exception as e:
            logger.warning("Cloud Vision OCR failed, falling back to Tesseract: %s", e)

    return ocr_image_tesseract(image_path)
