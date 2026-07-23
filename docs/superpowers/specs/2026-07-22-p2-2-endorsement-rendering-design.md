# P2-2 — Endorsement, Slip-Sheets, Produced-Document Rendering

**Date:** 2026-07-22
**Phase:** 2 (Production / Deliverable Output), sub-project P2-2
**Depends on:** P2-1 (production sets + Bates assignment, PR #38 — stacked branch), P1-2/3 (redaction burn-in)
**Consumed by:** P2-3 (DAT/OPT + manifest + ZIP packaging reference the persisted artifacts), P2-4 (UI)

## Decision context (brainstormed 2026-07-22)

- **Render = persisted artifacts, not streaming.** P2-3 packaging and
  defensibility need durable output; this is the first code path that writes
  assembled PDFs to storage (`upload_bytes`, `application/pdf`).
- **Per-PAGE Bates.** Every produced page carries its own sequential number
  (doc `bates_begin` + page offset) — that is what Bates stamping means.
  Numbers derive from the P2-1 lock snapshot; nothing is renumbered here.
- **Disposition drives the page pipeline** (from the item snapshot, never
  recomputed): `withhold` → single slip-sheet page; `redact_in_part` → burn
  redactions THEN stamp; `produce` → stamp only.
- **Job orchestration mirrors ingest**: Cloud Tasks batches with OIDC-guarded
  worker endpoint, inline BackgroundTasks fallback for dev. Status lives on
  `production_sets` columns; per-item progress = items with `output_path`.
- **Re-render allowed** after completion or error; artifact paths are
  deterministic so re-render overwrites. 409 only while a render is running
  or the set is still draft.
- **Stamps are black text on a small white backing box** — legible on dark
  scans. Fonts: DejaVu via the existing try/except fallback pattern.
- **Malpractice guard:** rendering reads ONLY `image_paths` renditions —
  never native files or text — and redactions are burned before stamping, so
  redacted pixels cannot reach a produced PDF.

## 1. Data model (one migration, import-safe: no `app.*` imports)

- `production_sets.render_status` — String(20), NOT NULL, server_default
  `'not_started'` — `not_started` | `rendering` | `rendered` | `error`.
- `production_sets.render_error` — Text, nullable.
- `production_sets.rendered_at` — DateTime, nullable.
- `production_set_items.output_path` — String(500), nullable — GCS path of
  the rendered PDF once produced.

`down_revision = "a9b8c7d6e5f4"` (P2-1 migration; current head on this branch).

## 2. Pure endorsement service — `app/services/endorse.py` (Pillow only, no DB/network)

```python
SLIP_W, SLIP_H = 1240, 1754  # A4 @ ~150 DPI, matches documents.py index pages

def page_bates_numbers(bates_begin: str, prefix: str, padding: int,
                       page_count: int) -> list[str]
# Parse the numeric tail of bates_begin (strip prefix, int()) and return
# [format_bates(prefix, n + i, padding) for i in range(page_count)].
# Reuses format_bates from production_numbering — never re-derives numbering.

def stamp_page(img: Image.Image, bates_text: str,
               designation: str | None) -> Image.Image
# Returns a stamped copy in RGB. Bates bottom-right, designation bottom-left
# (skipped when None/empty). Black text on a white backing box, 8px padding,
# anchored ~1.5% from page edges; font size scales with page height
# (max(14, h // 60)), DejaVu-Bold via try/except fallback to load_default.

def slip_sheet(bates_text: str, designation: str | None,
               title: str = "DOCUMENT WITHHELD") -> Image.Image
# White SLIP_W x SLIP_H page, title centered in large type, then stamped via
# stamp_page so the Bates/designation land in the standard corners.
```

## 3. Render pipeline — `app/services/production_render.py` (DB + storage aware)

```python
def artifact_path(production_id: int, set_id: int, bates_begin: str) -> str
# f"productions/{production_id}/production_sets/{set_id}/{bates_begin}.pdf"

async def render_item(db, ps: ProductionSet, item: ProductionSetItem) -> str
# Renders ONE member to a PDF, uploads it, sets item.output_path, returns
# the path. Disposition switch:
#   withhold      -> [slip_sheet(first_page_bates, designation)]
#   redact_in_part-> load pages, burn_page(img, reds_by_page), stamp each
#   produce       -> load pages, stamp each
# Page loading matches documents.py: image_paths entries starting with
# "productions/" come from storage.get_download_bytes, else local files;
# unreadable pages are skipped (rendered PDF still produced if >= 1 page;
# zero readable pages raises RuntimeError naming the doc).
# Stamping: texts from page_bates_numbers(...); designation = item.designation
# or ps.designation. PDF assembly: first.save(buf, format="PDF",
# save_all=True, append_images=rest, resolution=150.0);
# storage.upload_bytes(buf.getvalue(), path, "application/pdf").

async def render_batch(db, set_id: int, document_ids: list[UUID]) -> int
# Worker unit: loads the set + named items, renders each via render_item,
# commits after each item (progress durability), returns rendered count.
# On exception: set ps.render_status = "error", ps.render_error = str(exc),
# commit, re-raise nothing (worker returns 200 so tasks don't retry forever).

async def finalize_if_complete(db, set_id: int) -> bool
# If every item has output_path and status == "rendering": status =
# "rendered", rendered_at = now, clear render_error. Called by the worker
# after each batch. Returns whether it finalized.
```

Redactions for `redact_in_part` docs are fetched per doc and bucketed by
`page_num` (1-based), exactly like `documents.py:549-558`.

## 4. Task fan-out — `app/services/tasks.py`

`enqueue_render_batch(set_id: int, document_ids: list[str])` modeled on
`enqueue_ingest_batch`: OIDC-authed POST to
`/api/production-sets/render-batch`, dispatch deadline 1800s. Batch size 25
documents (constant in the router).

## 5. Endpoints — `app/routers/production_sets.py` (extend)

- `POST /api/production-sets/{set_id}/render` — manager+. 409 if
  `status != "locked"`; 409 if `render_status == "rendering"`. Sets
  `render_status="rendering"`, clears `render_error` and every item's
  `output_path` (re-render semantics), audit-logs, commits; then enqueues
  batches via Cloud Tasks when `tasks.is_configured()`, else runs
  `render_batch` chunks through FastAPI `BackgroundTasks`. Returns
  `{"documents": N, "batches": M}`.
- `POST /api/production-sets/render-batch` — worker endpoint, guarded by
  `Depends(verify_cloud_tasks_request)`, body `{set_id, document_ids}`;
  calls `render_batch` then `finalize_if_complete`. Returns `{"rendered": n}`.
- `GET /api/production-sets/{set_id}/documents/{document_id}/pdf` — any role
  with matter access. 404 if the item has no `output_path`. GCS paths
  redirect (307) to `get_signed_url(output_path)`; the endpoint exists for
  spot-checking rendered output before P2-3 packages it.
- `GET /production-sets/{set_id}` (existing detail) — response gains
  `render_status`, `render_error`, `rendered_at`, `rendered_count` (items
  with output_path). `ProductionSetOut` grows these fields (defaults:
  `"not_started"`, None, None, 0) so P2-1 list responses stay valid.

## 6. Error handling

- Render trigger on a draft set → 409; concurrent trigger → 409 (status
  check); worker failures set `render_status="error"` + message, and a
  subsequent render trigger may retry (409 only guards `rendering`).
- A doc with zero readable pages fails the batch (better loud than a silent
  hole in a production); the error names the document's control number.
- Per-item commits mean a crashed worker resumes cleanly on re-render.

## 7. Testing (fake-session pattern; pure Pillow tests use tiny in-memory images)

- Pure (`test_endorse.py`): `page_bates_numbers` sequence/prefix/padding
  (incl. overflow past padding); `stamp_page` returns a copy, corner regions
  contain the white backing box (sample pixels), designation omitted when
  None; `slip_sheet` dimensions + mostly-white + stamped corners.
- Pipeline (`test_production_render.py`): `artifact_path` layout;
  `render_item` disposition switch (slip-sheet page count 1, burn called for
  redact_in_part only, stamp for all) with monkeypatched storage/loader;
  zero-readable-pages raises; `finalize_if_complete` flips status only when
  all items have paths.
- Endpoints (append to `test_production_set_endpoints.py`): render 409s
  (draft/running), re-render clears paths, trigger returns batch math,
  worker guarded path updates counts, pdf endpoint 404-without-path.
- Migration purity + single head; full backend suite green (known
  `test_ai_review` failure excepted).

## Out of scope (later sub-projects)

- DAT/OPT load files, manifest, Bates-continuity report, ZIP packaging,
  produced searchable text / re-OCR — P2-3.
- Builder/render UI — P2-4. TIFF output — deferred behind PDF.
