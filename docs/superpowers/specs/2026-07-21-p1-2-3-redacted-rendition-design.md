# P1-2/3 — Redacted Rendition ("View as Produced")

**Date:** 2026-07-21
**Phase:** 1 (Redaction & Privilege), sub-projects P1-2 + P1-3
**Depends on:** P1-1 (Redaction model + CRUD, shipped)
**Consumed by:** P1-4 (redaction QC), Phase 2 (production output)

## Decision context

The original P1-2/3 framing was automatic role-based suppression: readonly users
get burned images and suppressed text/search/exports. During brainstorming we
established that readonly users are own-side viewers (client, co-counsel,
experts) — opposing counsel never has an app login; they receive the Phase-2
production deliverable. Blocking your own client from their own documents is
mostly incoherent (the client owns the privilege), so **automatic in-app
suppression is out of scope**. The malpractice-level enforcement point
("produced un-redacted privileged text") lives in Phase 2's production
pipeline, which will reuse the machinery built here.

**What P1-2/3 builds instead:** redaction burn-in as an explicit, opt-in
rendition — a "view as produced" mode available to anyone with document
access — plus the pure burn-in service Phase 2 will call at production time.

Superseded during brainstorming (recorded so they aren't re-litigated):
role-based visibility, excluding redacted docs from readonly search, and
closing native/summary/chat vectors are all deferred to Phase 2 (production
enforcement) or a future per-production toggle if a protective-order use case
ever materializes.

## 1. Burn-in service

New module `backend/app/services/redaction_render.py`:

```python
def burn_page(img: PIL.Image.Image, rects: Sequence[RectLike]) -> PIL.Image.Image
```

- `RectLike` = anything with `x_pct, y_pct, w_pct, h_pct, reason_code`
  (the `Redaction` model qualifies; Phase 2 can pass plain tuples/dataclasses).
- Draws an opaque black rectangle for each rect, converting 0–100 normalized
  percentages to pixel coordinates against the image's actual size.
- Stamps the reason code in white text centered in the box when the box is
  large enough to fit it legibly; omits the label otherwise. Reason-code labels
  are what privilege logs cross-reference, and the black-box-with-label look is
  the industry standard.
- Pure: no DB, no network, no storage. Returns a new image; does not mutate
  the input.
- Font loading follows the existing pattern in `GET /documents/{id}/pdf`
  (DejaVu from the Docker image, PIL bitmap fallback for dev).

Rejected alternatives:
- **PDF-layer redaction (pikepdf/PyMuPDF):** our PDFs are assembled from page
  images with no text layer, so there is nothing PDF-native to redact.
- **Pre-computed burned renditions in GCS:** cache invalidation complexity for
  a perf problem we don't have at current scale. Burn on demand.

## 2. Endpoints — one opt-in flag, three surfaces

All three accept `?redacted=1` (boolean query param, default off). With the
flag off, behavior is exactly today's. With the flag on and zero redactions,
the normal rendition is returned unchanged.

- `GET /documents/{id}/image/{page_num}?redacted=1` — loads the page image,
  burns that page's redactions, returns JPEG. Composes with the existing `w`
  thumbnail resize (burn first, then resize).
- `GET /documents/{id}/pdf?redacted=1` — burns each page's redactions and
  **omits annotation pins and the annotation index entirely** (annotations are
  work product; this is the as-produced view).
- `GET /documents/{id}/text?redacted=1` — if the document has any redactions,
  returns `{"text": "", "withheld": true}` instead of `text_content`.
  Region-level text removal is impossible today (OCR stores a flat
  `text_content` blob, no word coordinates); the honest as-produced text is
  re-OCR of burned images, which is Phase 2's job. With no redactions:
  `{"text": <normal>, "withheld": false}`.

Access control is unchanged: the same production-access check as today,
no role gating on the flag.

## 3. Detail payload

`DocumentDetail` gains `redaction_count: int` (same pattern as
`annotation_count`) so the viewer can offer a "view as produced" affordance
without an extra round trip.

## 4. Testing

TDD throughout (red-green-refactor per test):

**`burn_page` (pixel-level, pure):**
- pixels inside a rect are pure black; pixels outside are untouched
- multiple rects on one page; overlapping rects
- edge-hugging rects (x+w = 100, y+h = 100) stay in bounds
- label renders (non-black pixels present inside a large box); a box too
  small for the label is solid black with no label pixels
- input image not mutated

**Endpoints:**
- flag off ⇒ byte-identical behavior to today (image, pdf, text)
- `?redacted=1` image: burned region black in returned JPEG
- `?redacted=1` pdf: pages burned; no pins; no annotation index pages
- `?redacted=1` text: placeholder iff redactions exist; `withheld` flag correct
- `?redacted=1` with zero redactions ⇒ normal rendition
- `redaction_count` present and correct in `DocumentDetail`

## Out of scope (explicit)

- Any automatic suppression by role (search exclusion, native blocking,
  summary/chat gating, CSV export changes) — Phase 2 / future toggle.
- `bulk-zip` redacted variant — Phase 2's production builder covers it.
- Re-OCR of burned images for as-produced text — Phase 2.
- Redaction QC workflow — P1-4.
- No schema migration needed: `redaction_count` is computed, not stored.
