# P1-6: Layout-Aware OCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consume Cloud Vision's full annotation (structure + word boxes) instead of its flat text: layout-aware `text_content`, per-page word-box sidecars in GCS, and an `ocr_paths` column — across both ingest paths and the re-OCR endpoints, then backfill prod.

**Architecture:** A new `PageOcr` dataclass in `app/services/ocr.py` carries reconstructed text + percent-coordinate word boxes per page. A shared `ocr_pages_with_sidecars()` helper in `app/services/ingest.py` OCRs page JPEGs and uploads one JSON sidecar per page; the TIFF path, PDF path, and both re-OCR handlers all flow through the same shapes. Born-digital PDF pages get boxes from PyMuPDF's text layer (no API cost) in the identical sidecar format.

**Tech Stack:** FastAPI, SQLAlchemy async + Alembic, google-cloud-vision (`document_text_detection`), PyMuPDF (`fitz`), Firebase Storage (GCS), pytest with monkeypatch (no DB in unit tests).

**Spec:** `docs/superpowers/specs/2026-07-24-p1-6-layout-aware-ocr-design.md` — read it first.

## Global Constraints

- Branch: `feat/p1-6-layout-aware-ocr` (exists, tracks origin/main). Ship via PR to `main`; never commit to `main` directly.
- The Alembic migration must be import-safe under minimal deps: it may import only `alembic`, `sqlalchemy`, and stdlib — no `app.*` imports (CI runs alembic without pydantic installed; a violation costs a failed deploy).
- Word-box coordinate system everywhere: percent-of-page floats in [0, 100], keys `{"t", "x", "y", "w", "h", "c"}`, matching `validate_rect` in `backend/app/services/redaction.py` (x+w ≤ 100, y+h ≤ 100).
- Sidecar JSON shape: `{"v": 1, "width": <px>, "height": <px>, "words": [...]}` at `productions/{production_id}/ocr/{stem}.json` where `stem` equals the page JPEG's filename stem.
- `ocr_paths` is always index-aligned with `image_paths`; `""` marks a page whose OCR or sidecar upload failed.
- Best-effort semantics: no per-page OCR/sidecar failure may fail an ingest or re-OCR batch.
- All backend tests run from `backend/`: `python -m pytest tests -q`. No frontend changes in this project.
- `app/services/ocr.py` must stay importable without google-cloud-vision installed (keep `from google.cloud import vision` lazy, inside functions).

## File Structure

- `backend/app/services/ocr.py` — modify: add `PageOcr`, annotation parsing, sidecar helpers, new `ocr_page_vision_bytes()`; existing `ocr_image_vision_bytes()` becomes a `.text` wrapper. Tesseract functions untouched.
- `backend/tests/test_ocr_layout.py` — create: unit tests for parsing/serialization (no network, fixtures via `SimpleNamespace`).
- `backend/app/models.py` — modify: `ocr_paths` column on `Document`.
- `backend/alembic/versions/y7g8h9i0j1k2_add_document_ocr_paths.py` — create.
- `backend/app/services/ingest.py` — modify: new `ocr_pages_with_sidecars()` helper; `process_ingest_record` uses it.
- `backend/tests/test_ingest_ocr_paths.py` — create: helper + TIFF-path tests.
- `backend/app/services/ingest_pdf.py` — modify: `iter_pdf_pages` yields `PageOcr`, `_pdf_words_pct()` for born-digital boxes, `process_pdf_record` uploads sidecars.
- `backend/tests/test_ingest_pdf.py` — modify: update 3-tuple contract tests; add box tests.
- `backend/app/routers/ingest.py` — modify: `reocr_batch_handler` and `run_reocr` use the shared helper and write `ocr_paths`.
- `backend/tests/test_reocr_layout.py` — create: re-OCR handler test.

---

### Task 1: PageOcr, annotation parsing, and sidecar helpers in `ocr.py`

**Files:**
- Modify: `backend/app/services/ocr.py`
- Test: `backend/tests/test_ocr_layout.py` (create)

**Interfaces:**
- Consumes: nothing new (Vision client usage unchanged).
- Produces (later tasks rely on these exact names):
  - `@dataclass PageOcr: text: str = ""; words: list = field(default_factory=list); width: int = 0; height: int = 0`
  - `page_ocr_from_annotation(fta) -> PageOcr` — pure, takes a `full_text_annotation`-shaped object.
  - `ocr_page_vision_bytes(image_bytes: bytes) -> PageOcr` — calls Vision.
  - `ocr_image_vision_bytes(image_bytes: bytes) -> str` — now returns `ocr_page_vision_bytes(...).text`.
  - `sidecar_bytes(page: PageOcr) -> bytes` — JSON `{"v": 1, "width", "height", "words"}`.
  - `sidecar_remote_path(production_id: int, stem: str) -> str` — `productions/{pid}/ocr/{stem}.json`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ocr_layout.py`. Fixtures mimic the Vision protobuf via `SimpleNamespace` (attribute access only — the parser must not require real protobufs):

```python
"""Unit tests for layout-aware parsing of Vision full_text_annotation."""

import json
from types import SimpleNamespace as NS

from app.services.ocr import (
    PageOcr,
    page_ocr_from_annotation,
    sidecar_bytes,
    sidecar_remote_path,
)

# Vision TextAnnotation.DetectedBreak.BreakType enum ints
SPACE, SURE_SPACE, EOL_SURE_SPACE, HYPHEN, LINE_BREAK = 1, 2, 3, 4, 5


def _word(text, box, brk=SPACE, confidence=0.9):
    """One Vision word: one symbol per char, detected_break on the last."""
    symbols = [NS(text=c, property=None) for c in text]
    symbols[-1] = NS(
        text=text[-1],
        property=NS(detected_break=NS(type_=brk)),
    )
    x0, y0, x1, y1 = box
    vertices = [NS(x=x0, y=y0), NS(x=x1, y=y0), NS(x=x1, y=y1), NS(x=x0, y=y1)]
    return NS(symbols=symbols, bounding_box=NS(vertices=vertices), confidence=confidence)


def _annotation(blocks, width=1000, height=2000, text="fallback"):
    page = NS(width=width, height=height, blocks=blocks)
    return NS(pages=[page], text=text)


def _para(*words):
    return NS(words=list(words))


def _block(*paras):
    return NS(paragraphs=list(paras))


def test_words_joined_with_spaces_and_line_breaks():
    fta = _annotation([
        _block(_para(
            _word("Dear", (0, 0, 100, 20)),
            _word("counsel,", (110, 0, 300, 20), brk=LINE_BREAK),
            _word("hello", (0, 30, 100, 50), brk=LINE_BREAK),
        )),
    ])
    page = page_ocr_from_annotation(fta)
    assert page.text == "Dear counsel,\nhello"


def test_paragraphs_newline_blocks_blank_line():
    fta = _annotation([
        _block(
            _para(_word("Para1", (0, 0, 50, 10), brk=LINE_BREAK)),
            _para(_word("Para2", (0, 20, 50, 30), brk=LINE_BREAK)),
        ),
        _block(_para(_word("Block2", (0, 100, 50, 110), brk=LINE_BREAK))),
    ])
    page = page_ocr_from_annotation(fta)
    assert page.text == "Para1\nPara2\n\nBlock2"


def test_hyphen_break_joins_without_space():
    fta = _annotation([
        _block(_para(
            _word("privi", (0, 0, 50, 10), brk=HYPHEN),
            _word("leged", (0, 12, 50, 22), brk=LINE_BREAK),
        )),
    ])
    page = page_ocr_from_annotation(fta)
    assert page.text == "privi-leged"


def test_word_boxes_are_percent_envelope():
    # 1000x2000 page; box 100..300 x, 200..400 y -> x=10%, y=10%, w=20%, h=10%
    fta = _annotation([
        _block(_para(_word("Hi", (100, 200, 300, 400), confidence=0.987))),
    ])
    page = page_ocr_from_annotation(fta)
    assert page.width == 1000 and page.height == 2000
    assert page.words == [
        {"t": "Hi", "x": 10.0, "y": 10.0, "w": 20.0, "h": 10.0, "c": 0.99}
    ]


def test_skewed_vertices_use_min_max_envelope():
    # Rotated quad: envelope is min/max over all four corners
    vertices = [NS(x=110, y=200), NS(x=300, y=210), NS(x=290, y=400), NS(x=100, y=390)]
    word = NS(
        symbols=[NS(text="X", property=NS(detected_break=NS(type_=LINE_BREAK)))],
        bounding_box=NS(vertices=vertices),
        confidence=0.5,
    )
    fta = _annotation([_block(_para(word))])
    box = page_ocr_from_annotation(fta).words[0]
    assert (box["x"], box["y"], box["w"], box["h"]) == (10.0, 10.0, 20.0, 10.0)


def test_out_of_page_coords_clamped_degenerate_dropped():
    fta = _annotation([
        _block(_para(
            _word("clamped", (-50, -50, 500, 100)),          # clamps to 0..50%, 0..5%
            _word("gone", (999999, 0, 999999, 0), brk=LINE_BREAK),  # degenerate -> dropped
        )),
    ])
    page = page_ocr_from_annotation(fta)
    assert [w["t"] for w in page.words] == ["clamped"]
    w = page.words[0]
    assert w["x"] == 0.0 and w["y"] == 0.0 and w["w"] == 50.0 and w["h"] == 5.0
    # dropped from words but still present in text
    assert "gone" in page.text


def test_no_page_structure_falls_back_to_flat_text():
    fta = NS(pages=[], text="  flat text  ")
    page = page_ocr_from_annotation(fta)
    assert page == PageOcr(text="flat text", words=[], width=0, height=0)


def test_none_annotation_yields_empty():
    assert page_ocr_from_annotation(None) == PageOcr()


def test_sidecar_bytes_shape():
    page = PageOcr(text="ignored", words=[{"t": "a", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0, "c": 0.9}], width=10, height=20)
    payload = json.loads(sidecar_bytes(page))
    assert payload == {
        "v": 1, "width": 10, "height": 20,
        "words": [{"t": "a", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0, "c": 0.9}],
    }


def test_sidecar_remote_path():
    assert sidecar_remote_path(7, "SMITH_000001_0001") == "productions/7/ocr/SMITH_000001_0001.json"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_ocr_layout.py -v`
Expected: FAIL — `ImportError: cannot import name 'PageOcr'`.

- [ ] **Step 3: Implement in `backend/app/services/ocr.py`**

Add after the existing imports/logger (keep `ocr_image_vision`, `ocr_image_tesseract`, `ocr_image` untouched):

```python
import json
from dataclasses import dataclass, field

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
    if x1 <= x0 or y1 <= y0:
        return None
    return {
        "t": "".join(s.text for s in word.symbols),
        "x": round(x0, 2),
        "y": round(y0, 2),
        "w": round(x1 - x0, 2),
        "h": round(y1 - y0, 2),
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
```

Then replace the body of the existing `ocr_image_vision_bytes` with:

```python
def ocr_image_vision_bytes(image_bytes: bytes) -> str:
    """Extract text from image bytes using Google Cloud Vision API."""
    return ocr_page_vision_bytes(image_bytes).text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_ocr_layout.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the whole suite (no regressions from the wrapper change)**

Run: `cd backend && python -m pytest tests -q`
Expected: same pass/fail baseline as before this task (no new failures).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/ocr.py backend/tests/test_ocr_layout.py
git commit -m "feat(p1-6): PageOcr layout parsing + sidecar helpers in ocr service"
```

---

### Task 2: `ocr_paths` column — model + migration

**Files:**
- Modify: `backend/app/models.py` (Document, after `raw_image_paths` around line 121)
- Create: `backend/alembic/versions/y7g8h9i0j1k2_add_document_ocr_paths.py`

**Interfaces:**
- Produces: `Document.ocr_paths` — JSONB list, NOT NULL, default `[]`, index-aligned with `image_paths`.

- [ ] **Step 1: Confirm the alembic head**

Run: `cd backend && python -m alembic heads`
Expected: single head `x6f7g8h9i0j1`. If a different single head is shown (parallel session may have merged work), use that revision as `down_revision` below.

- [ ] **Step 2: Add the model column**

In `backend/app/models.py`, directly below `raw_image_paths = Column(JSONB, nullable=False, default=list)`:

```python
    # P1-6 — per-page word-box sidecar paths, index-aligned with image_paths
    ocr_paths = Column(JSONB, nullable=False, default=list)
```

- [ ] **Step 3: Write the migration**

Create `backend/alembic/versions/y7g8h9i0j1k2_add_document_ocr_paths.py` (imports only alembic/sqlalchemy — import-safe under minimal deps):

```python
"""add ocr_paths to documents

Per-page OCR word-box sidecar paths (GCS), index-aligned with image_paths.
"" marks a page whose OCR or sidecar upload failed.

Revision ID: y7g8h9i0j1k2
Revises: x6f7g8h9i0j1
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "y7g8h9i0j1k2"
down_revision = "x6f7g8h9i0j1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "documents",
        sa.Column(
            "ocr_paths",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("documents", "ocr_paths")
```

- [ ] **Step 4: Verify import-safety and revision graph**

Run: `cd backend && python -c "import runpy; runpy.run_path('alembic/versions/y7g8h9i0j1k2_add_document_ocr_paths.py')" && python -m alembic heads`
Expected: no ImportError; single head is now `y7g8h9i0j1k2`.

- [ ] **Step 5: Run the suite**

Run: `cd backend && python -m pytest tests -q`
Expected: baseline, no new failures.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/y7g8h9i0j1k2_add_document_ocr_paths.py
git commit -m "feat(p1-6): documents.ocr_paths column + migration"
```

---

### Task 3: shared OCR helper + TIFF/opticon path

**Files:**
- Modify: `backend/app/services/ingest.py` (`process_ingest_record`, OCR block around lines 369-384; `Document(...)` around line 392)
- Test: `backend/tests/test_ingest_ocr_paths.py` (create)

**Interfaces:**
- Consumes: `ocr_page_vision_bytes`, `sidecar_bytes`, `sidecar_remote_path` from Task 1 (lazy-imported inside the helper so tests can monkeypatch `app.services.ocr.*`); `upload_bytes`, `get_download_bytes` from `app.services.storage`.
- Produces: `ocr_pages_with_sidecars(production_id: int, jpeg_paths: list[str], label: str, errors: list[str]) -> tuple[list[str], list[str]]` in `app/services/ingest.py` — returns `(page_texts, ocr_paths)`; `page_texts` holds non-empty page texts in order; `ocr_paths` aligns index-for-index with `jpeg_paths` (`""` on any failure). Task 5 reuses this exact function.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ingest_ocr_paths.py`:

```python
"""Tests for the shared page-OCR + sidecar helper (P1-6)."""

import app.services.ocr as ocr_mod
import app.services.storage as storage_mod
from app.services.ingest import ocr_pages_with_sidecars
from app.services.ocr import PageOcr


def _happy_ocr(jpeg_bytes):
    return PageOcr(text="Page text", words=[{"t": "Page", "x": 1.0, "y": 1.0, "w": 5.0, "h": 2.0, "c": 0.9}], width=100, height=200)


def test_uploads_sidecar_per_page_and_aligns_paths(monkeypatch):
    uploaded = {}
    monkeypatch.setattr(storage_mod, "get_download_bytes", lambda p: b"jpeg")
    monkeypatch.setattr(
        storage_mod, "upload_bytes",
        lambda data, remote, content_type=None: uploaded.setdefault(remote, data) or remote,
    )
    monkeypatch.setattr(ocr_mod, "ocr_page_vision_bytes", _happy_ocr)

    errors: list[str] = []
    texts, ocr_paths = ocr_pages_with_sidecars(
        7,
        ["productions/7/converted/A_0001.jpg", "", "productions/7/converted/A_0003.jpg"],
        "SMITH 000001",
        errors,
    )

    assert texts == ["Page text", "Page text"]
    # "" jpeg placeholder stays "" in ocr_paths; alignment preserved
    assert ocr_paths == [
        "productions/7/ocr/A_0001.json",
        "",
        "productions/7/ocr/A_0003.json",
    ]
    assert set(uploaded) == {"productions/7/ocr/A_0001.json", "productions/7/ocr/A_0003.json"}
    assert errors == []


def test_ocr_failure_appends_error_and_empty_path(monkeypatch):
    def boom(jpeg_bytes):
        raise RuntimeError("Vision API error: quota")

    monkeypatch.setattr(storage_mod, "get_download_bytes", lambda p: b"jpeg")
    monkeypatch.setattr(ocr_mod, "ocr_page_vision_bytes", boom)

    errors: list[str] = []
    texts, ocr_paths = ocr_pages_with_sidecars(7, ["productions/7/converted/A_0001.jpg"], "SMITH 000001", errors)

    assert texts == []
    assert ocr_paths == [""]
    assert len(errors) == 1 and "SMITH 000001" in errors[0]


def test_sidecar_upload_failure_keeps_text_marks_path_empty(monkeypatch):
    def bad_upload(data, remote, content_type=None):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(storage_mod, "get_download_bytes", lambda p: b"jpeg")
    monkeypatch.setattr(storage_mod, "upload_bytes", bad_upload)
    monkeypatch.setattr(ocr_mod, "ocr_page_vision_bytes", _happy_ocr)

    errors: list[str] = []
    texts, ocr_paths = ocr_pages_with_sidecars(7, ["productions/7/converted/A_0001.jpg"], "SMITH 000001", errors)

    assert texts == ["Page text"]  # text survives a sidecar failure
    assert ocr_paths == [""]
    assert len(errors) == 1 and "sidecar" in errors[0]


def test_blank_page_still_writes_sidecar(monkeypatch):
    """OCR ran, found nothing: sidecar with words=[] distinguishes this from failure."""
    uploaded = []
    monkeypatch.setattr(storage_mod, "get_download_bytes", lambda p: b"jpeg")
    monkeypatch.setattr(
        storage_mod, "upload_bytes",
        lambda data, remote, content_type=None: uploaded.append(remote) or remote,
    )
    monkeypatch.setattr(
        ocr_mod, "ocr_page_vision_bytes",
        lambda b: PageOcr(text="", words=[], width=100, height=200),
    )

    errors: list[str] = []
    texts, ocr_paths = ocr_pages_with_sidecars(7, ["productions/7/converted/A_0001.jpg"], "SMITH 000001", errors)

    assert texts == []  # empty text not joined into text_content
    assert ocr_paths == ["productions/7/ocr/A_0001.json"]
    assert uploaded == ["productions/7/ocr/A_0001.json"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_ingest_ocr_paths.py -v`
Expected: FAIL — `ImportError: cannot import name 'ocr_pages_with_sidecars'`.

- [ ] **Step 3: Implement the helper**

In `backend/app/services/ingest.py`, add this module-level function directly above `process_ingest_record` (note: `Path` is already imported at the top of this file; the `app.services.*` imports are deliberately inside the function so monkeypatching the source modules works):

```python
def ocr_pages_with_sidecars(
    production_id: int,
    jpeg_paths: list[str],
    label: str,
    errors: list[str],
) -> tuple[list[str], list[str]]:
    """OCR converted page JPEGs and upload one word-box sidecar per page.

    Returns (page_texts, ocr_paths). ocr_paths aligns index-for-index with
    jpeg_paths; "" marks a page whose OCR or sidecar upload failed. A blank
    page that OCR'd successfully still gets a sidecar (words=[]), so sidecar
    presence means "OCR ran". Best-effort: never raises.
    """
    from app.services import ocr as ocr_service
    from app.services import storage as storage_service

    page_texts: list[str] = []
    ocr_paths: list[str] = []
    for jpeg_path in jpeg_paths:
        if not jpeg_path:
            ocr_paths.append("")
            continue
        try:
            jpeg_bytes = storage_service.get_download_bytes(jpeg_path)
            page = ocr_service.ocr_page_vision_bytes(jpeg_bytes)
        except Exception as e:
            errors.append(f"{label}: Vision OCR failed for {jpeg_path}: {e}")
            ocr_paths.append("")
            continue
        if page.text:
            page_texts.append(page.text)
        try:
            remote = ocr_service.sidecar_remote_path(production_id, Path(jpeg_path).stem)
            storage_service.upload_bytes(
                ocr_service.sidecar_bytes(page), remote, content_type="application/json"
            )
            ocr_paths.append(remote)
        except Exception as e:
            errors.append(f"{label}: sidecar upload failed for {jpeg_path}: {e}")
            ocr_paths.append("")
    return page_texts, ocr_paths
```

- [ ] **Step 4: Wire `process_ingest_record` through it**

In `process_ingest_record`, replace the whole "Run Cloud Vision OCR on converted images" block (the `vision_text_parts` loop and the `if vision_text_parts:` join, currently lines 369-384) with:

```python
    # Run Cloud Vision OCR on converted images for higher-quality text
    vision_text_parts, ocr_paths = ocr_pages_with_sidecars(
        production_id, jpeg_storage_paths, bates_begin, errors
    )
    if vision_text_parts:
        text_content = "\n\n".join(vision_text_parts)
```

and add `ocr_paths=ocr_paths,` to the `Document(...)` constructor call (after `image_paths=jpeg_storage_paths,`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_ingest_ocr_paths.py tests/test_pipeline.py tests/test_ingest_skip_idempotency.py -v`
Expected: new tests PASS; existing ingest tests unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/ingest.py backend/tests/test_ingest_ocr_paths.py
git commit -m "feat(p1-6): shared page-OCR sidecar helper; TIFF path writes ocr_paths"
```

---

### Task 4: PDF path — `PageOcr` per page, born-digital boxes from PyMuPDF

**Files:**
- Modify: `backend/app/services/ingest_pdf.py` (`iter_pdf_pages`, `_ocr_jpeg`, `process_pdf_record`; imports at top)
- Test: `backend/tests/test_ingest_pdf.py` (modify existing tests + add new)

**Interfaces:**
- Consumes: `PageOcr`, `sidecar_bytes`, `sidecar_remote_path` from Task 1 (top-level import — `ocr.py` has no heavy top-level deps).
- Produces: `iter_pdf_pages(pdf_bytes, ocr_fn, dpi) -> Iterator[tuple[int, bytes, PageOcr | None]]` — `PageOcr` for extracted/OCR'd pages, `None` when `ocr_fn` failed (page gets `""` in `ocr_paths`, no sidecar). `ocr_fn: Callable[[bytes], PageOcr | None]`. `_pdf_words_pct(page) -> list[dict]` (born-digital boxes, `c` = 1.0). `process_pdf_record` returns Documents with `ocr_paths` aligned to `image_paths`.

- [ ] **Step 1: Update the existing contract tests and add new ones**

In `backend/tests/test_ingest_pdf.py`:

Add to the imports at the top:

```python
from app.services.ocr import PageOcr
```

Replace `test_born_digital_uses_embedded_text_and_skips_ocr`:

```python
def test_born_digital_uses_embedded_text_and_skips_ocr():
    ocr_calls = []

    def fake_ocr(jpeg_bytes: bytes) -> PageOcr:
        ocr_calls.append(jpeg_bytes)
        return PageOcr(text="SHOULD-NOT-BE-USED")

    pages = list(iter_pdf_pages(_born_digital_pdf("Hello discovery"), ocr_fn=fake_ocr))

    assert len(pages) == 1
    page_num, jpeg, page_ocr = pages[0]
    assert page_num == 1
    assert jpeg[:3] == b"\xff\xd8\xff"  # JPEG magic bytes
    assert "Hello discovery" in page_ocr.text
    assert ocr_calls == []  # OCR not invoked for born-digital text


def test_born_digital_words_have_percent_boxes():
    pages = list(iter_pdf_pages(_born_digital_pdf("Hello discovery"), ocr_fn=lambda b: PageOcr()))
    page_ocr = pages[0][2]

    assert page_ocr.width > 0 and page_ocr.height > 0
    texts = [w["t"] for w in page_ocr.words]
    assert texts == ["Hello", "discovery"]
    for w in page_ocr.words:
        assert 0.0 <= w["x"] <= 100.0 and 0.0 <= w["y"] <= 100.0
        assert w["w"] > 0.0 and w["h"] > 0.0
        assert w["x"] + w["w"] <= 100.0 and w["y"] + w["h"] <= 100.0
        assert w["c"] == 1.0  # embedded text layer is exact
```

Replace `test_scanned_page_falls_back_to_ocr`:

```python
def test_scanned_page_falls_back_to_ocr():
    def fake_ocr(jpeg_bytes: bytes) -> PageOcr:
        return PageOcr(text="OCR-RECOVERED-TEXT")

    pages = list(iter_pdf_pages(_blank_two_page_pdf(), ocr_fn=fake_ocr))

    assert len(pages) == 2
    combined = "\n\n".join(p.text for _, _, p in pages)
    assert combined.count("OCR-RECOVERED-TEXT") == 2
```

Replace `test_pages_rendered_for_every_page`:

```python
def test_pages_rendered_for_every_page():
    pages = list(iter_pdf_pages(_blank_two_page_pdf(), ocr_fn=lambda b: PageOcr()))
    assert [p[0] for p in pages] == [1, 2]  # page numbers, every page yielded
```

Replace `test_process_pdf_record_assembles_document`:

```python
def test_process_pdf_record_assembles_document(monkeypatch):
    item = {
        "storage_path": "productions/7/raw/A/first.pdf",
        "relative_path": "A/first.pdf",
        "filename": "first.pdf",
    }

    monkeypatch.setattr(pdf_mod, "get_download_bytes", lambda path: b"%PDF-fake")
    monkeypatch.setattr(
        pdf_mod,
        "iter_pdf_pages",
        lambda pdf_bytes, ocr_fn, dpi=pdf_mod.RENDER_DPI: iter(
            [
                (1, b"\xff\xd8jpeg1", PageOcr(text="extracted text", words=[], width=100, height=200)),
                (2, b"\xff\xd8jpeg2", PageOcr()),
            ]
        ),
    )
    uploaded = []
    monkeypatch.setattr(
        pdf_mod,
        "upload_bytes",
        lambda data, remote, content_type=None: uploaded.append(remote) or remote,
    )

    errors: list[str] = []
    doc = process_pdf_record(
        production_id=7,
        item=item,
        global_index=0,
        prefix="SMITH",
        errors=errors,
    )

    assert doc.bates_begin == "SMITH 000001"
    assert doc.bates_end == "SMITH 000001"
    assert doc.page_count == 2
    assert doc.title == "first"
    assert doc.text_content == "extracted text"
    assert doc.metadata_["File Name"] == "first.pdf"
    assert doc.metadata_["Folder"] == "A"
    assert doc.native_path == "productions/7/raw/A/first.pdf"
    assert doc.image_paths == [
        "productions/7/converted/SMITH_000001_0001.jpg",
        "productions/7/converted/SMITH_000001_0002.jpg",
    ]
    # one sidecar per page, stem matches the page JPEG
    assert doc.ocr_paths == [
        "productions/7/ocr/SMITH_000001_0001.json",
        "productions/7/ocr/SMITH_000001_0002.json",
    ]
    assert uploaded == [
        "productions/7/converted/SMITH_000001_0001.jpg",
        "productions/7/ocr/SMITH_000001_0001.json",
        "productions/7/converted/SMITH_000001_0002.jpg",
        "productions/7/ocr/SMITH_000001_0002.json",
    ]
    assert errors == []


def test_process_pdf_record_ocr_failure_marks_empty_ocr_path(monkeypatch):
    """ocr_fn returned None (Vision failed): no sidecar, '' placeholder."""
    item = {
        "storage_path": "productions/7/raw/A/first.pdf",
        "relative_path": "A/first.pdf",
        "filename": "first.pdf",
    }
    monkeypatch.setattr(pdf_mod, "get_download_bytes", lambda path: b"%PDF-fake")
    monkeypatch.setattr(
        pdf_mod,
        "iter_pdf_pages",
        lambda pdf_bytes, ocr_fn, dpi=pdf_mod.RENDER_DPI: iter([(1, b"\xff\xd8jpeg1", None)]),
    )
    monkeypatch.setattr(
        pdf_mod, "upload_bytes", lambda data, remote, content_type=None: remote
    )

    errors: list[str] = []
    doc = process_pdf_record(7, item, 0, "SMITH", errors)

    assert doc.text_content is None
    assert doc.ocr_paths == [""]
```

- [ ] **Step 2: Run tests to verify the new/changed ones fail**

Run: `cd backend && python -m pytest tests/test_ingest_pdf.py -v`
Expected: the updated/new tests FAIL (tuple unpacking / missing `ocr_paths`); the pure-string tests (`derive_bates_prefix` etc.) still PASS.

- [ ] **Step 3: Implement in `backend/app/services/ingest_pdf.py`**

Add to the top-level imports:

```python
from app.services.ocr import PageOcr, sidecar_bytes, sidecar_remote_path
```

Add below `looks_like_bates_stub`:

```python
def _pdf_words_pct(page: "fitz.Page") -> list[dict]:
    """Word boxes from the embedded text layer, as percent-of-page coords.

    PyMuPDF reports PDF points relative to page.rect; the rendered JPEG has
    the same aspect, so percent coordinates transfer directly.
    """
    rect = page.rect
    if not rect.width or not rect.height:
        return []
    words: list[dict] = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        if not text.strip():
            continue
        xp0 = max(0.0, min(100.0, x0 / rect.width * 100))
        xp1 = max(0.0, min(100.0, x1 / rect.width * 100))
        yp0 = max(0.0, min(100.0, y0 / rect.height * 100))
        yp1 = max(0.0, min(100.0, y1 / rect.height * 100))
        if xp1 <= xp0 or yp1 <= yp0:
            continue
        words.append({
            "t": text,
            "x": round(xp0, 2),
            "y": round(yp0, 2),
            "w": round(xp1 - xp0, 2),
            "h": round(yp1 - yp0, 2),
            "c": 1.0,
        })
    return words
```

Replace `iter_pdf_pages` (same memory-bounded structure, new yield type):

```python
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
```

Replace `_ocr_jpeg`:

```python
def _ocr_jpeg(jpeg_bytes: bytes) -> PageOcr | None:
    """OCR a single rendered page via Cloud Vision. None on failure."""
    try:
        from app.services.ocr import ocr_page_vision_bytes

        return ocr_page_vision_bytes(jpeg_bytes)
    except Exception:
        logger.exception("Vision OCR failed for a rendered PDF page")
        return None
```

In `process_pdf_record`, replace the page loop (currently the `for page_num, jpeg, page_text in iter_pdf_pages(...)` block) with:

```python
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
            page_stem = f"{control_number.replace(' ', '_')}_{page_num:04d}"
            remote = f"productions/{production_id}/converted/{page_stem}.jpg"
            try:
                upload_bytes(jpeg, remote, content_type="image/jpeg")
                image_paths.append(remote)
            except Exception as e:
                errors.append(f"{control_number}: image upload failed page {page_num}: {e}")
                image_paths.append("")
            if page_ocr is None:
                ocr_paths.append("")  # OCR failed: distinguishable from words=[]
            else:
                try:
                    sidecar_remote = sidecar_remote_path(production_id, page_stem)
                    upload_bytes(
                        sidecar_bytes(page_ocr), sidecar_remote,
                        content_type="application/json",
                    )
                    ocr_paths.append(sidecar_remote)
                except Exception as e:
                    errors.append(
                        f"{control_number}: sidecar upload failed page {page_num}: {e}"
                    )
                    ocr_paths.append("")
    except Exception as e:
        errors.append(f"{control_number}: failed to render {relative_path}: {e}")
        return None
```

(The `remote` path expression is unchanged in effect — `page_stem` is exactly the old inline f-string's stem, now shared with the sidecar path.)

Finally add `ocr_paths=ocr_paths,` to the `Document(...)` constructor (after `image_paths=image_paths,`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_ingest_pdf.py tests/test_zip_intake.py tests/test_multi_load.py -v`
Expected: all PASS (zip/multi-load touch the PDF path's neighbors).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ingest_pdf.py backend/tests/test_ingest_pdf.py
git commit -m "feat(p1-6): PDF pages yield PageOcr; born-digital boxes from PyMuPDF; sidecars"
```

---

### Task 5: re-OCR endpoints use the layout pipeline

**Files:**
- Modify: `backend/app/routers/ingest.py` (`reocr_batch_handler` around line 481, `run_reocr` around line 535)
- Test: `backend/tests/test_reocr_layout.py` (create)

**Interfaces:**
- Consumes: `ocr_pages_with_sidecars` from Task 3, imported INSIDE each handler body (`from app.services import ingest as ingest_service`) so tests can monkeypatch `app.services.ingest.ocr_pages_with_sidecars`.
- Produces: both handlers set `doc.ocr_paths` (always, when the doc has images) and `doc.text_content` + tsvector (when any page text came back).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_reocr_layout.py`:

```python
"""Re-OCR endpoints write layout text + ocr_paths (P1-6)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import app.services.ingest as ingest_mod
from app.routers.ingest import reocr_batch_handler


def _doc():
    doc = MagicMock()
    doc.id = "d0000000-0000-0000-0000-000000000001"
    doc.production_id = 7
    doc.bates_begin = "SMITH 000001"
    doc.image_paths = ["productions/7/converted/SMITH_000001_0001.jpg"]
    return doc


def _db_returning(docs):
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = docs
    db = MagicMock()
    # first execute() = the SELECT; later calls = tsvector UPDATEs
    db.execute = AsyncMock(side_effect=[select_result, MagicMock(), MagicMock()])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def test_reocr_batch_writes_layout_text_and_ocr_paths(monkeypatch):
    doc = _doc()
    monkeypatch.setattr(
        ingest_mod,
        "ocr_pages_with_sidecars",
        lambda pid, paths, label, errors: (
            ["Structured\n\ntext"],
            ["productions/7/ocr/SMITH_000001_0001.json"],
        ),
    )
    db = _db_returning([doc])

    asyncio.run(reocr_batch_handler({"production_id": 7}, db=db, _verified=None))

    assert doc.text_content == "Structured\n\ntext"
    assert doc.ocr_paths == ["productions/7/ocr/SMITH_000001_0001.json"]
    assert db.execute.await_count == 2  # SELECT + tsvector UPDATE
    db.commit.assert_awaited()


def test_reocr_batch_blank_pages_still_persist_ocr_paths(monkeypatch):
    """No text recovered: ocr_paths still recorded, text_content untouched."""
    doc = _doc()
    doc.text_content = "old text"
    monkeypatch.setattr(
        ingest_mod,
        "ocr_pages_with_sidecars",
        lambda pid, paths, label, errors: ([], ["productions/7/ocr/SMITH_000001_0001.json"]),
    )
    db = _db_returning([doc])

    asyncio.run(reocr_batch_handler({"production_id": 7}, db=db, _verified=None))

    assert doc.text_content == "old text"
    assert doc.ocr_paths == ["productions/7/ocr/SMITH_000001_0001.json"]
    assert db.execute.await_count == 1  # SELECT only, no tsvector UPDATE
    db.commit.assert_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reocr_layout.py -v`
Expected: FAIL — `doc.ocr_paths` is a bare MagicMock attribute, not the expected list (handler doesn't set it yet).

- [ ] **Step 3: Implement in `backend/app/routers/ingest.py`**

In `reocr_batch_handler`, replace the imports and per-doc loop. The function body becomes:

```python
    """Cloud Tasks worker: re-OCR a batch of documents."""
    from app.services import ingest as ingest_service

    production_id = body.get("production_id")
    offset = body.get("offset", 0)
    limit = body.get("limit", 25)

    result = await db.execute(
        select(Document)
        .where(Document.production_id == production_id)
        .order_by(Document.id)
        .offset(offset)
        .limit(limit)
    )
    docs = list(result.scalars().all())
    logger.info("Re-OCR batch: production %d, offset %d, %d docs", production_id, offset, len(docs))

    for doc in docs:
        try:
            if not doc.image_paths:
                continue
            page_errors: list[str] = []
            text_parts, ocr_paths = ingest_service.ocr_pages_with_sidecars(
                doc.production_id, doc.image_paths, doc.bates_begin, page_errors
            )
            for err in page_errors:
                logger.warning("Re-OCR: %s", err)
            doc.ocr_paths = ocr_paths
            if text_parts:
                doc.text_content = "\n\n".join(text_parts)
                await db.execute(
                    text(
                        "UPDATE documents SET text_search_vector = "
                        "to_tsvector('english', COALESCE(:txt, '')) "
                        "WHERE id = :id"
                    ),
                    {"txt": doc.text_content, "id": doc.id},
                )
            await db.commit()
        except Exception:
            logger.exception("Re-OCR failed for doc %s", doc.bates_begin)
            await db.rollback()

    return {"ok": True, "processed": len(docs)}
```

In `run_reocr`, the function becomes (deleting its `ocr_image_vision_bytes`/`get_download_bytes` imports; the docstring, logs, and rollback shape are the existing ones):

```python
async def run_reocr(production_id: int):
    """Background task fallback: re-OCR all documents in a production using Cloud Vision."""
    from app.database import async_session_factory
    from app.services import ingest as ingest_service

    async with async_session_factory() as db:
        result = await db.execute(
            select(Document).where(Document.production_id == production_id)
        )
        docs = list(result.scalars().all())
        logger.info("Re-OCR: processing %d documents for production %d", len(docs), production_id)

        for i, doc in enumerate(docs):
            try:
                if not doc.image_paths:
                    continue
                page_errors: list[str] = []
                text_parts, ocr_paths = ingest_service.ocr_pages_with_sidecars(
                    production_id, doc.image_paths, doc.bates_begin, page_errors
                )
                for err in page_errors:
                    logger.warning("Re-OCR: %s", err)
                doc.ocr_paths = ocr_paths
                if text_parts:
                    doc.text_content = "\n\n".join(text_parts)
                    await db.execute(
                        text(
                            "UPDATE documents SET text_search_vector = "
                            "to_tsvector('english', COALESCE(:txt, '')) "
                            "WHERE id = :id"
                        ),
                        {"txt": doc.text_content, "id": doc.id},
                    )
                await db.commit()
                if (i + 1) % 25 == 0:
                    logger.info("Re-OCR: %d/%d done", i + 1, len(docs))
            except Exception:
                logger.exception("Re-OCR failed for doc %s", doc.bates_begin)
                await db.rollback()

        logger.info("Re-OCR complete for production %d", production_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_reocr_layout.py tests/test_route_registration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ingest.py backend/tests/test_reocr_layout.py
git commit -m "feat(p1-6): re-OCR endpoints write layout text + ocr_paths sidecars"
```

---

### Task 6: full suite, PR

**Files:** none new.

- [ ] **Step 1: Run the entire backend suite**

Run: `cd backend && python -m pytest tests -q`
Expected: zero new failures vs the baseline from Task 1 Step 5. Investigate and fix any regression before proceeding (do NOT skip or xfail).

- [ ] **Step 2: Grep for stragglers**

Run: `grep -rn "ocr_image_vision_bytes" backend/app`
Expected: only the definition in `ocr.py` remains (all four call sites migrated). If a call site appears (parallel-session drift), migrate it the same way as Task 3/5 before continuing.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/p1-6-layout-aware-ocr
gh pr create --title "feat(p1-6): layout-aware OCR — structured text + word-box sidecars" --body "$(cat <<'EOF'
## Summary
- Parse Vision's full_text_annotation instead of flat text: paragraph/block-aware text_content
- Persist per-word bounding boxes (percent coords, matching redaction rects) as per-page GCS sidecars + documents.ocr_paths
- Born-digital PDF pages get boxes from PyMuPDF's text layer (no API cost)
- Re-OCR endpoints upgraded so the prod backfill populates existing docs

Spec: docs/superpowers/specs/2026-07-24-p1-6-layout-aware-ocr-design.md

## Test plan
- [ ] backend suite green (new: test_ocr_layout, test_ingest_ocr_paths, test_reocr_layout; updated: test_ingest_pdf)
- [ ] post-merge: prod migration + per-production re-OCR backfill (Task 7)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR opens against `main`; CI (including minimal-deps alembic check) green.

---

### Task 7: prod rollout — migration + backfill (operator steps, after PR merge)

**Files:** none. Requires prod access (gcloud as the prod account; app auth as the production owner). Coordinate with the user before starting; every step here touches prod.

- [ ] **Step 1: Wait for merge + Cloud Run deploy**

Merging to `main` auto-deploys Cloud Run. Confirm the new revision is serving before migrating (deploy details: memory `prod-deploy-and-migrations`).

- [ ] **Step 2: Run the prod migration**

Follow memory `prod-deploy-and-migrations` (manual alembic against Neon): run `alembic upgrade head` with the prod `DATABASE_URL`, then verify:

```sql
SELECT column_name FROM information_schema.columns
WHERE table_name = 'documents' AND column_name = 'ocr_paths';
```

Expected: one row. New ingests now persist `ocr_paths`.

- [ ] **Step 3: Trigger the backfill per production**

Enumerate production ids (`SELECT id, name FROM productions ORDER BY id;`), then for each id, as the production owner, call `POST /api/ingest/reocr/{production_id}` (via the app UI's session or an authenticated request). Cloud Tasks batches of 25 do the work. Cost check: corpus is ~1-4k pages → single-digit dollars of Vision calls.

- [ ] **Step 4: Verify**

- GCS: `productions/{id}/ocr/` contains one `.json` per page; spot-check one sidecar against its page JPEG (words' `x/y/w/h` land on the visible text).
- DB: `SELECT count(*) FROM documents WHERE ocr_paths != '[]'::jsonb;` grows to (near) the doc count; docs whose pages all failed keep `[""]`-style placeholders.
- App: run a search that previously returned mid-sentence snippets; confirm snippets/paragraph breaks look sane and AI chat still grounds on document text.

- [ ] **Step 5: Update program-status memory**

Record in the master-status memory that P1-6 shipped and the backfill ran (or which productions remain).

---

## Self-Review Notes

- Spec coverage: §1 parsing → Task 1; §2 sidecar + column → Tasks 1-2; §3 call sites → Tasks 3-5; §4 backfill → Task 7; §5 error handling → Tasks 3-5 tests; §6 testing → per-task TDD steps. Non-goals honored (no new endpoint, no Document AI, no Tesseract boxes, no frontend).
- Spec deviation (deliberate, small): in the PDF path an `ocr_fn` failure yields `page_ocr = None` → `""` placeholder and no sidecar, exactly matching the spec's "sidecar presence means OCR ran". The TIFF path implements the same rule inside `ocr_pages_with_sidecars`.
- Type consistency: `PageOcr` fields and `{"t","x","y","w","h","c"}` keys are identical across Tasks 1, 3, 4; `ocr_pages_with_sidecars(production_id, jpeg_paths, label, errors)` signature identical in Tasks 3 and 5.
