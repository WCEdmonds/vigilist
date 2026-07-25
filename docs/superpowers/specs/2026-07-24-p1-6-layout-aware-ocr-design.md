# P1-6: Layout-Aware OCR — Design

**Date:** 2026-07-24
**Status:** Approved
**Depends on:** P1-1 redaction model (percent-rect coordinate system)

## Problem

Both OCR paths already call Cloud Vision's layout-aware endpoint
(`document_text_detection`), but every call site keeps only
`full_text_annotation.text` — a flat string — and discards the rest of the
response: the page → block → paragraph → word hierarchy, per-word bounding
boxes, and confidence scores.

Two costs follow:

1. `text_content` has no reliable paragraph/block structure, which degrades
   search snippets and what AI review/chat sees.
2. Word bounding boxes are destroyed at ingest. They are the prerequisite for
   search-and-redact and PII auto-suggest in the redaction workflow, and
   recovering them later means paying to re-OCR every page (~$1.50/1k).

## Decisions (settled with user)

- **Scope:** structured text reconstruction AND per-word box persistence, in
  this sub-project.
- **Backfill:** upgrade the existing re-OCR endpoints to the new pipeline,
  then run the backfill across existing prod productions as part of rollout.
- **Box storage:** GCS sidecar JSON per page + an `ocr_paths` column
  mirroring `image_paths` (option A). A dense page is 300–500 words
  (~30–50KB JSON); a 1,000-page document would be tens of MB — unacceptable
  as a Postgres row on Neon, fine as page-granular objects fetched on demand.
- **No new API:** Cloud Vision stays; Document AI (Layout Parser, $10/1k, no
  free tier) is deferred until tables/forms extraction is actually needed.

## Design

### 1. OCR service returns structure (`backend/app/services/ocr.py`)

New dataclass and entry point:

```python
@dataclass
class PageOcr:
    text: str               # layout-aware reconstruction
    words: list[dict]       # [{"t", "x", "y", "w", "h", "c"}, ...]
    width: int              # source image px
    height: int

def ocr_page_vision_bytes(image_bytes: bytes) -> PageOcr: ...
```

`ocr_page_vision_bytes` calls `document_text_detection` exactly as today and
consumes the annotation hierarchy:

- **Text reconstruction.** Walk `pages[].blocks[].paragraphs[].words[]` in
  Vision's returned order. Join words per the `detected_break` on each
  word's last symbol: `SPACE`/`SURE_SPACE` → `" "`,
  `EOL_SURE_SPACE`/`LINE_BREAK` → `"\n"`, `HYPHEN` → join with the printed
  hyphen and no space or break. Paragraph boundary → `"\n"`, block
  boundary → `"\n\n"`.
- **Words.** Each word becomes `{"t": text, "x", "y", "w", "h", "c"}` where
  x/y/w/h are percent-of-page floats taken from the axis-aligned envelope
  of the word's bounding-box pixel vertices (min/max over the four corners,
  which absorbs slight skew) and clamped to [0, 100], and `c` is confidence
  rounded to 2 places. This is deliberately the same coordinate system as
  `validate_rect` (P1-1, `backend/app/services/redaction.py`): a word box
  is a valid redaction rect with zero translation.
- **Fallback.** If `full_text_annotation` has no page structure, return the
  flat `.text` and `words=[]` — output no worse than today.

`ocr_image_vision_bytes` becomes a thin wrapper returning
`ocr_page_vision_bytes(...).text`; `ocr_image_vision`, `ocr_image`, and the
Tesseract fallback are untouched (Tesseract produces no boxes).

### 2. Sidecar format and storage

Per page, one JSON object:

```json
{"v": 1, "width": 2125, "height": 2750,
 "words": [{"t": "PRIVILEGED", "x": 12.1, "y": 4.4, "w": 9.6, "h": 1.4, "c": 0.98}]}
```

- Uploaded to `productions/{production_id}/ocr/{stem}.json`, where `stem`
  matches the page's converted JPEG filename stem. A sidecar is written
  whenever OCR ran — even with `words: []` (e.g., blank page or
  structureless fallback) — so sidecar presence distinguishes "OCR ran,
  nothing found" from the `""` failure placeholder.
- New column: `documents.ocr_paths` JSONB, NOT NULL, server default `[]` —
  a list strictly parallel to `image_paths`, with `""` placeholders where
  OCR or the sidecar upload failed (same convention as `image_paths`).
- One Alembic migration, import-safe under CI's minimal-deps alembic run
  (no imports from app modules that require pydantic etc.).

### 3. Call-site changes (all four)

- **TIFF/opticon path** (`backend/app/services/ingest.py`): for each
  converted JPEG, run `ocr_page_vision_bytes`, upload the sidecar, append
  to `ocr_paths` (aligned index-for-index with `jpeg_storage_paths`).
  `text_content` remains `"\n\n".join(non-empty page texts)`.
- **PDF path** (`backend/app/services/ingest_pdf.py`): `iter_pdf_pages`
  yields `(page_number, jpeg_bytes, page_ocr)` where `page_ocr` is a
  `PageOcr` regardless of engine:
  - Born-digital pages (embedded text ≥ `MIN_TEXT_CHARS`): text from
    `page.get_text()` as today; words from `page.get_text("words")`
    converted to the same percent schema via `page.rect`. Zero API cost.
  - Scanned pages: Vision via `ocr_page_vision_bytes`.
  `process_pdf_record` uploads one sidecar per page and sets `ocr_paths`.
  Downstream consumers cannot tell which engine produced a page.
- **Re-OCR endpoints** (`backend/app/routers/ingest.py`: `reocr_batch_handler`
  and `run_reocr`): switch to the new pipeline; write structured
  `text_content`, sidecars, and `ocr_paths`; keep the
  `text_search_vector` refresh.

### 4. Prod backfill (rollout step)

After deploy, trigger the existing `/ingest/reocr/{production_id}` for each
current production. At the current corpus (~1–4k pages/month volume) this is
single-digit dollars. It rewrites `text_content` with structure and
populates sidecars + `ocr_paths` for every existing document. Verification:
sample several documents across productions and check sidecar boxes visually
against page images, and confirm search still returns sensible snippets.

Born-digital PDF documents are re-OCR'd via Vision on their rendered JPEGs
(the re-OCR path has no PDF in hand); this costs a few Vision calls but
yields consistent boxes. Acceptable at current corpus size.

### 5. Error handling

Best-effort per page, preserving current semantics: any per-page OCR
failure or sidecar upload failure logs, appends `""` to `ocr_paths`, and
never fails the ingest or re-OCR batch. Box percents are clamped to
[0, 100]; degenerate boxes (zero/negative extent after clamping) are
dropped from `words`. Consumers of `text_content` (search vector, AI
review, chat) see no structural change — only better line/paragraph breaks.

### 6. Testing

- **Reconstruction unit tests** from canned `full_text_annotation`-shaped
  fixtures: break types (space, line, paragraph, block), hyphenation
  joining, block ordering, percent conversion + clamping, and the
  structureless fallback.
- **PyMuPDF word extraction** against a tiny generated PDF: words land in
  percent coordinates matching `page.rect`.
- **Call-site tests** with a mocked Vision client: sidecars uploaded to the
  right paths; `ocr_paths` aligns index-for-index with `image_paths`
  including `""` failure placeholders; re-OCR batch updates `text_content`,
  `ocr_paths`, and the search vector.
- TDD during implementation.

## Non-goals

- No endpoint serving boxes to the frontend (the redaction-UI sub-project
  adds it; sidecars in GCS are its data source).
- No Document AI migration.
- No boxes from the Tesseract fallback.
- No frontend changes.
