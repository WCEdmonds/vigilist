# Redaction Drawing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let reviewers draw redaction boxes on emails, PDF and image attachments, and produce exactly what they redacted, with nothing under the boxes leaking through text, metadata or a stale render.

**Architecture:** A new imaging step turns imageless documents into page images plus word sidecars (emails via PyMuPDF `fitz.Story` HTML→PDF; PDFs and images through the existing `ingest_pdf` helpers), run on the worker via Cloud Tasks, before lock. The viewer gains a Redact mode over the page images, built on the existing redaction API. Production gains leak guards: drift detection between lock and render, an unlock, new validation conflicts, text with redacted words removed, and DAT header-field blanking.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PyMuPDF (`fitz`), Pillow, Cloud Tasks; React + TypeScript (Vite).

**Spec:** `docs/superpowers/specs/2026-11-01-redaction-drawing-design.md`. Read it first; this plan argues from it.

## Global Constraints

- Redaction and word-box coordinates are **0–100 page percentages**, top-left origin (existing convention).
- Imaging stores pages exactly like ingested PDFs: `upload_page_assets(production_id, control_number=doc.bates_begin, ...)` → `productions/{pid}/converted/{stem}_{page:04d}.jpg` and `productions/{pid}/ocr/{stem}.json`, sidecar v1 `{"v":1,"width","height","words":[{t,x,y,w,h,c}]}`.
- Render DPI = `ingest_pdf.RENDER_DPI` (250). Email pages = US Letter (`fitz.paper_rect("letter")`), 54 pt margins.
- Email header date shown in UTC, written as `YYYY-MM-DD HH:MM UTC`.
- No network fetches while laying out an email: remote `src`/`href` resources removed before layout.
- Heavy work runs on the worker via `services/tasks._enqueue`; worker endpoints use `Depends(verify_cloud_tasks_request)` and always return 200 (record failures in the row).
- **Page images are frozen once a document has any redaction**: imaging returns 409 / raises `ImagingRefused`.
- Redaction create/update refuses (409) while `imaging_status in ("queued", "running")`; `page_num` is validated against `len(image_paths)` when the document has images.
- Word removal threshold: a word is redacted when ≥ 0.30 of its area lies under a redaction box. Each run of consecutive redacted words → one `[REDACTED]`; a whole-page box (0,0,100,100) → `[REDACTED PAGE]`.
- If any page of a redacted document lacks a readable sidecar → ship **no** text, empty TEXTPATH. Never fall back to `text_content`.
- Unlock: manager+, only when `package_status` is null or `"error"`.
- Office files (doc/docx/xls/xlsx/ppt/pptx/other) are **not imageable**.
- Tests and fixtures use ACME-style fakes only (no real matter data).
- Frontend: no test runner; verify with `npm run build` and `npx eslint <touched files>` (main already lints red — touched files must not add errors). React Compiler rules: no refs or setState during render.
- Migrations import only `alembic`/`sqlalchemy` (CI runs them under minimal deps).

## Review Focus

1. **An email with no body and no HTML** (headers only) must still image to one page with a header block, and header regions. → test in Task 2.
2. **A pathological email** (tens of thousands of paragraphs) must not run the worker out of memory: cap at `MAX_IMAGED_PAGES = 500` and fail with "too many pages to image (N)". → test in Task 2.
3. **A box clipping the edge of a word**: < 30 % covered stays, ≥ 30 % goes; a box over half a line removes only the covered words. → tests in Task 8.
4. **Double-click "Prepare pages" / bulk over already-queued documents** must not enqueue twice or reset a running job. → test in Task 4.
5. **A redaction saved while imaging is running**, or imaging requested after a box exists, is refused with a message, never silently re-imaged under the boxes. → tests in Tasks 3 and 5.

---

### Task 1: Schema for imaging state and lock snapshot

**Files:**
- Create: `backend/alembic/versions/d2l3m4n5o6p7_imaging_and_redaction_snapshot.py` (revision `d2l3m4n5o6p7`, down_revision `c1k2l3m4n5o6`)
- Modify: `backend/app/models.py` (`Document`, `ProductionSetItem`)
- Modify: `backend/app/schemas.py` (`DocumentDetail`)
- Modify: `backend/app/routers/documents.py` (detail endpoint near the `redaction_count` query, ~line 1407)
- Test: `backend/tests/test_imaging_schema.py`

**Interfaces:**
- Produces: `Document.imaging_status: str|None` (`queued|running|done|failed`), `Document.imaging_error: str|None` (String 500), `Document.imaged_from: str|None` (`email|pdf|image`), `Document.header_regions: dict|None` (JSONB, `{field: {"page": int, "x","y","w","h": float}}`), `Document.imaging_requested_at: datetime|None`, `ProductionSetItem.redaction_count: int|None`. `DocumentDetail` gains `imaging_status`, `imaging_error`, `imageable: bool`, `image_count: int`.

- [ ] **Step 1: Write the failing test** — `test_document_detail_imaging_defaults`: `DocumentDetail(**minimal fields)` has `imaging_status is None`, `imaging_error is None`, `imageable is False`, `image_count == 0`; and `test_models_have_imaging_columns` asserts the new attributes exist on `Document` and `ProductionSetItem`.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_imaging_schema.py` → FAIL (fields missing).
- [ ] **Step 3: Add columns, migration, schema fields** (all nullable; `DocumentDetail` defaults `None, None, False, 0`). The detail endpoint fills `image_count=len(doc.image_paths or [])`; `imageable` is filled in Task 3.
- [ ] **Step 4: Run** the test and `python -m pytest -q` → PASS, suite green.
- [ ] **Step 5: Commit** `feat(redaction): imaging state columns and lock redaction snapshot`.

### Task 2: Email page layout

**Files:**
- Create: `backend/app/services/email_pages.py`
- Test: `backend/tests/test_email_pages.py`

**Interfaces:**
- Produces:
  - `HEADER_FIELDS = ("from", "to", "cc", "bcc", "date", "subject", "attachments")`
  - `MAX_IMAGED_PAGES = 500`
  - `class EmailPages(NamedTuple): pdf: bytes; header_regions: dict[str, dict]; page_count: int`
  - `sanitize_body_html(html: str) -> str` — removes `script`, `style`, `iframe`, `object`, `embed`, `form`, `link`, `meta` elements; removes any `src`/`href`/`background` attribute whose value is not a `data:` URI; removes `display:none`/`visibility:hidden` inline styles.
  - `email_html(doc, attachment_names: list[str]) -> str` — header block (one element per non-empty field, each with `id="hdr-<field>"`), then sanitized body HTML, else `text_content` in `<pre>`.
  - `render_email_pages(doc, attachment_names: list[str]) -> EmailPages` — raises `ValueError("too many pages to image (N)")` past the cap.

Header block copy (exact labels): `From:`, `To:`, `Cc:`, `Bcc:`, `Date:`, `Subject:`, `Attachments:`; attachments joined with `"; "`; date as `YYYY-MM-DD HH:MM UTC` from `doc.date_sent or doc.date_received`.

- [ ] **Step 1: Write failing tests**
  - `test_header_regions_cover_each_present_field`: ACME email with from/to/subject/date, two attachment names → `header_regions` keys == `{"from","to","date","subject","attachments"}`, every region `page == 1`, `0 <= x,y` and `x+w <= 100`, `y+h <= 100`, subject region's `y` > from region's `y`.
  - `test_long_body_spans_pages_and_has_text_layer`: 200 `<p>` paragraphs → `page_count > 1`; `fitz.open(stream=pdf)` page 1 `get_text()` contains `"Subject:"` and the subject text.
  - `test_headers_only_email_is_one_page` (Review Focus 1): no html, no text → `page_count == 1`, regions include `subject`.
  - `test_remote_resources_and_scripts_are_stripped`: `<img src="http://evil.example/x.png"><script>x()</script><img src="data:image/png;base64,iVBORw0KGgo=">` → output has no `http://evil.example`, no `<script`, keeps the `data:` image.
  - `test_page_cap` (Review Focus 2): monkeypatch `MAX_IMAGED_PAGES = 2` with a 200-paragraph body → `pytest.raises(ValueError, match="too many pages")`.
  - `test_non_ascii_subject_survives`: subject `"Café – Q3"` appears in page 1 text.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_email_pages.py` → FAIL (module missing).
- [ ] **Step 3: Implement.** Layout loop as in the spec spike: `fitz.Story(html=...)`, `fitz.DocumentWriter(BytesIO)`, `story.place(where)` / `story.draw(dev)` per page; break and raise once pages exceed the cap. Header regions: `story.element_positions(callback)` collects rects for `id`s starting `hdr-` on page 1; convert points → percentages of `fitz.paper_rect("letter")`. Sanitize with the stdlib `html.parser` (no new dependency).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(redaction): lay out emails as pages with header regions`.

### Task 3: Imaging service

**Files:**
- Create: `backend/app/services/imaging.py`
- Modify: `backend/app/routers/documents.py` (detail: `imageable=imaging_kind(doc) is not None`)
- Test: `backend/tests/test_imaging_service.py`

**Interfaces:**
- Consumes: `email_pages.render_email_pages`, `ingest_pdf.iter_pdf_pages`, `ingest_pdf.upload_page_assets`, `ingest_pdf._ocr_jpeg`, `storage.get_download_bytes`.
- Produces:
  - `IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}`
  - `imaging_kind(doc) -> Literal["email", "pdf", "image"] | None` — `email` when `file_type == "email"`; `pdf` when the native (`native_path`) ends `.pdf`; `image` for `IMAGE_EXTS`; `None` when the doc already has `image_paths`, or has no native (non-email), or anything else.
  - `class ImagingRefused(Exception)`
  - `async def image_document(db, doc_id: UUID) -> None` — the worker entry point.
  - `def build_pages(doc, kind, attachment_names, *, ocr_fn, store_fn) -> tuple[list[str], list[str], int, dict | None]` — pure-ish core: returns `(image_paths, ocr_paths, page_count, header_regions)`; `store_fn(bytes, path, content_type)` is injected (tests pass a dict sink).

Behaviour of `image_document`: load doc; if it has any `Redaction` rows → set `imaging_status="failed"`, `imaging_error="has redactions; pages are frozen"`, raise `ImagingRefused`; set `running`, **commit** (release the connection before heavy work — PR #87/#88 lesson); `build_pages` in `asyncio.to_thread`; on success write `image_paths`, `ocr_paths`, `page_count`, `imaged_from`, `header_regions`, `imaging_status="done"`, `imaging_error=None` in one commit; on exception write `failed` + `str(e)[:500]`. Attachment names for emails = `file_name` of family members (same `production_id`, same `family_id`, excluding the parent), ordered by `bates_begin`. Multi-frame TIFF → one page per frame; each image page is OCR'd with `ocr_fn` for word boxes.

- [ ] **Step 1: Write failing tests** (fake storage sink, fake `ocr_fn` returning `PageOcr(words=[...])`):
  - `test_imaging_kind_table`: email → `"email"`; attachment `native_path="…/budget.pdf"` → `"pdf"`; `.PNG` → `"image"`; `.xlsx` → `None`; doc with `image_paths=["x"]` → `None`; non-email without native → `None`.
  - `test_build_pages_email_uploads_index_aligned_assets`: returns equal-length `image_paths`/`ocr_paths`, `page_count == len(image_paths)`, header_regions present, every stored key under `productions/{pid}/converted/` or `/ocr/`.
  - `test_build_pages_pdf_uses_native_pages`: 3-page in-memory PDF via `fitz` → 3 pages, `header_regions is None`.
  - `test_build_pages_multiframe_tiff`: 2-frame TIFF → 2 pages.
  - `test_image_document_refuses_when_redactions_exist` (Review Focus 5): FakeSession with a redaction row → `ImagingRefused`, status `failed`, nothing stored.
  - `test_image_document_records_failure`: `build_pages` raising → status `failed`, error recorded, no exception escapes except via return.
  - `test_image_document_commits_before_heavy_work`: commit count ≥ 1 before `build_pages` is called (record order with a spy).
- [ ] **Step 2: Run** `python -m pytest -q tests/test_imaging_service.py` → FAIL.
- [ ] **Step 3: Implement** per the interface above.
- [ ] **Step 4: Run** the tests + full suite → PASS.
- [ ] **Step 5: Commit** `feat(redaction): image emails, PDFs and images on demand`.

### Task 4: Prepare-pages endpoints and worker task

**Files:**
- Create: `backend/app/routers/imaging.py`; register in `backend/app/main.py`
- Modify: `backend/app/services/tasks.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_imaging_endpoints.py`

**Interfaces:**
- Consumes: `imaging.imaging_kind`, `imaging.image_document`, `bulk_tagging.resolve_query_docs/resolve_explicit_docs/expand_attachments/MAX_BULK_DOCS`, `FolderAddDocuments` request shape (reuse it as `PreparePagesRequest = FolderAddDocuments`).
- Produces:
  - `tasks.enqueue_imaging(document_ids: list[str]) -> None` → `_enqueue("/api/documents/imaging-worker", {"document_ids": [...]})`; batch size 10 ids per task.
  - `POST /api/documents/{doc_id}/prepare-pages` → `{"imaging_status": "queued"}`; 409 `"This document has redactions; its pages are frozen"`; 422 `"Word, Excel and PowerPoint files can't be imaged yet"` (or `"Already has page images"`); reviewer+; idempotent when already `queued`/`running` and `imaging_requested_at` is under `STALE_IMAGING = timedelta(minutes=35)` old (returns current status, enqueues nothing); older than that counts as failed and is re-queued. Every queueing sets `imaging_requested_at = now`.
  - `POST /api/productions/{production_id}/prepare-pages` body `PreparePagesRequest` → `{"queued": n, "already_imaged": n, "in_progress": n, "unsupported": n, "has_redactions": n}`; query pinned to `production_id`; attachments included.
  - `POST /api/documents/imaging-worker` (Cloud Tasks) body `{"document_ids": [...]}` → images each sequentially, swallowing `ImagingRefused`; always 200.
  - Audit: `document_imaging_requested` (per request, with counts), `document_imaged` / `document_imaging_failed` (per doc, from the worker).

- [ ] **Step 1: Write failing tests**
  - `test_prepare_single_queues_and_enqueues_once` (Review Focus 4): first call sets `queued` and enqueues 1 task; second call returns `queued` and enqueues nothing.
  - `test_prepare_single_rejects_office_and_redacted` (422 / 409).
  - `test_stale_running_job_is_requeued`: `running` with `imaging_requested_at` 40 min ago → re-queued and enqueued.
  - `test_prepare_bulk_counts_and_skips`: docs in states (imageless email, already imaged, queued, xlsx, redacted) → counts `{1,1,1,1,1}`, only one id enqueued.
  - `test_prepare_bulk_query_pinned_to_production`.
  - `test_readonly_cannot_prepare` → 403.
  - `test_worker_images_each_and_returns_200_on_refusal`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Set `imaging_status="queued"` and commit **before** enqueueing; if enqueue raises, set `failed` with the message.
- [ ] **Step 4: Run** → PASS; full suite green.
- [ ] **Step 5: Commit** `feat(redaction): prepare-pages endpoints and imaging worker`.

### Task 5: Redaction API hardening

**Files:**
- Modify: `backend/app/routers/redactions.py`, `backend/app/services/redaction.py`
- Test: `backend/tests/test_redaction_endpoints.py` (extend)

**Interfaces:**
- Produces: `validate_rect(..., page_count)` unchanged signature; callers pass `page_limit(doc) -> int` = `len(doc.image_paths)` when non-empty else `doc.page_count` (new helper in `services/redaction.py`).

- [ ] **Step 1: Write failing tests**
  - `test_create_validates_against_image_count`: doc `page_count=1`, `image_paths` of 3 → page 3 accepted, page 4 → 422.
  - `test_create_refused_while_imaging` (Review Focus 5): `imaging_status="running"` → 409 `"Pages are being prepared"`; same for update.
  - `test_update_audit_records_production_and_before_after`: details contain `production_id`, `before` and `after` boxes.
  - `test_delete_audit_records_production_and_box`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix(redaction): validate pages against images, refuse during imaging, fuller audit`.

### Task 6: Validation conflicts and lock snapshot

**Files:**
- Modify: `backend/app/services/production_validation.py`, `backend/app/routers/production_sets.py` (lock, ~lines 357-470)
- Test: `backend/tests/test_production_validation.py`, `backend/tests/test_production_set_endpoints.py` (extend)

**Interfaces:**
- Consumes: `imaging.imaging_kind`, `redacted_metadata.blanked_fields` (Task 9 — implement the attachment check against it; until Task 9 lands, a local helper with the same signature is fine, then delete it).
- Produces: `compute_conflicts` buckets `needs_pages`, `no_images` (unimageable only), `redactions_overridden`, `attachment_names_redacted` (+ existing). `NON_OVERRIDABLE = frozenset({"needs_pages"})`. Lock sets `item.redaction_count` = current count per document.

Rules:
- `needs_pages`: non-withheld member, no images, `imaging_kind(doc) is not None` or `imaging_status in ("queued","running")`.
- `redactions_overridden`: member has redactions and `privilege_disposition == "produce"`.
- `attachment_names_redacted`: member is an attachment (`families.is_attachment`) whose parent (same production, `bates_begin == family_id`) has `"attachments" in blanked_fields(parent.header_regions, parent_redactions)` and the member's disposition is not `withhold`.
- Lock with `override_conflicts=True` still returns 409 while any `NON_OVERRIDABLE` bucket is non-empty, detail `"Prepare pages for N documents before locking"`.

- [ ] **Step 1: Write failing tests** — one per bucket (positive and negative), `test_lock_refuses_needs_pages_even_with_override`, `test_lock_snapshots_redaction_count`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS (update the exact-dict assertions in existing validation tests for the new keys).
- [ ] **Step 5: Commit** `feat(production): needs-pages, override and attachment-name conflicts; snapshot redactions at lock`.

### Task 7: Render drift guard and unlock

**Files:**
- Create: `backend/app/services/redaction_drift.py`
- Modify: `backend/app/routers/production_sets.py` (render endpoint ~line 477, new unlock route), `backend/app/services/production_render.py` (`render_item`)
- Modify: `backend/app/services/storage.py` only if `delete_prefix` doesn't already cover rendered outputs (it exists — reuse)
- Test: `backend/tests/test_redaction_drift.py`, `backend/tests/test_production_set_endpoints.py` (extend)

**Interfaces:**
- Produces:
  - `async def redaction_drift(db, ps, items) -> list[str]` — Bates begins of items where: current redaction count ≠ `item.redaction_count`; or any redaction `created_at`/`updated_at` ≥ `ps.locked_at`; or the doc has redactions and `item.disposition == "produce"` / `item.produce_native`.
  - `POST /api/production-sets/{set_id}/render` → 409 `"Redactions changed after lock for: A, B — unlock and lock again"` when drift is non-empty. `render_item` re-checks its own item and raises `RuntimeError` with the same message (belt and braces for tasks already queued).
  - `POST /api/production-sets/{set_id}/unlock` → `ProductionSetInfo`; manager+; 409 unless `status == "locked"` and `package_status in (None, "error")`; clears per-item `sort_order, bates_begin, bates_end, pages, disposition, produce_native, designation, redaction_count, output_path` and set `locked_by, locked_at, render_status, render_error, rendered_at, conflicts_overridden_by, conflicts_overridden_at`; `delete_prefix(artifact prefix for the set)` best-effort; audit `production_set_unlocked` with the released Bates range.

- [ ] **Step 1: Write failing tests** — drift cases (added after lock, edited after lock, deleted after lock via count, redactions on a `produce` item, clean set → `[]`); render 409; `render_item` raises; unlock happy path clears fields and logs; unlock 409 when packaged; unlock 403 for reviewer; re-lock after unlock assigns Bates from `start_number`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(production): refuse renders with post-lock redaction changes; add unlock`.

### Task 8: Text with redacted words removed

**Files:**
- Create: `backend/app/services/redacted_text.py`
- Modify: `backend/app/services/production_export.py` (`package_set` text blob ~line 248; `build_dat_rows` `has_text` ~line 89), `backend/app/routers/documents.py` (`/text?redacted=1` ~line 1334)
- Test: `backend/tests/test_redacted_text.py`, `backend/tests/test_production_export.py` (extend)

**Interfaces:**
- Produces:
  - `REDACTED = "[REDACTED]"`, `REDACTED_PAGE = "[REDACTED PAGE]"`, `WORD_COVER_THRESHOLD = 0.30`
  - `def covered_fraction(word: dict, rect) -> float`
  - `def page_text(words: list[dict], rects: list) -> str` — groups words into lines (sort by vertical centre; a word joins the current line when its centre is within half the line's median height of the line's centre; within a line sort by `x`), joins with spaces/newlines, collapses runs of redacted words into one `REDACTED`; whole-page rect → `REDACTED_PAGE`.
  - `def produced_text(sidecars: list[dict | None], redactions_by_page: dict[int, list]) -> str | None` — `None` if any sidecar is `None`; pages joined with `"\f"` (form feed, the usual page separator in production text).
  - `def load_sidecars(doc) -> list[dict | None]` — downloads each `ocr_paths` entry; empty path or unreadable JSON → `None`.
- `package_set`: for `redact_in_part`, `text_blob = produced_text(...)` encoded; DAT TEXTPATH set iff a blob was written (pass through `doc_paths`).
- `/text?redacted=1` returns `{"text": produced or "", "withheld": produced is None}`.

- [ ] **Step 1: Write failing tests** (Review Focus 3):
  - `test_word_under_box_is_removed_and_runs_collapse`: line "Pay account 4417 1234 today" with a box over "4417 1234" → `"Pay account [REDACTED] today"`.
  - `test_threshold`: box covering 25 % of a word keeps it; 35 % removes it.
  - `test_partial_line_box_removes_only_covered_words`.
  - `test_lines_group_by_vertical_position` (words given out of order).
  - `test_whole_page_box`.
  - `test_missing_sidecar_withholds_all_text` → `None`.
  - `test_package_ships_redacted_text_and_textpath` / `test_package_ships_no_text_when_a_sidecar_is_missing`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS (update `test_production_export.py::…no text for redacted…` expectation to the new behaviour).
- [ ] **Step 5: Commit** `feat(production): ship text with redacted words removed`.

### Task 9: DAT header-field blanking

**Files:**
- Create: `backend/app/services/redacted_metadata.py`
- Modify: `backend/app/services/loadfile_export.py` (`DAT_COLUMNS`: add `BCC` after `CC`, `REDACTEDFIELDS` after `REDACTED`), `backend/app/services/production_export.py` (`build_dat_rows`)
- Test: `backend/tests/test_redacted_metadata.py`, `backend/tests/test_production_export.py`, `backend/tests/test_loadfile_export.py` (extend)

**Interfaces:**
- Produces:
  - `FIELD_COLUMNS = {"from": "FROM", "to": "TO", "cc": "CC", "bcc": "BCC", "date": "DATESENT", "subject": "SUBJECT", "attachments": None}` (`attachments` blanks nothing on the parent's row; it drives Task 6's conflict).
  - `def blanked_fields(header_regions: dict | None, redactions: list) -> set[str]` — a field is blanked when any redaction on the region's page intersects its region with positive area.
- `build_dat_rows` for `redact_in_part`: blank each mapped column in `blanked_fields`; `REDACTEDFIELDS` = sorted column names joined with `"; "` (empty otherwise); `BCC` = `doc.email_bcc` (blank for withheld rows like SUBJECT).

- [ ] **Step 1: Write failing tests** — overlap / touching-edge-only (no blank) / other page; DAT row for a redacted email with the subject covered → `SUBJECT == ""`, `REDACTEDFIELDS == "SUBJECT"`, `FROM` intact; column order snapshot updated.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; replace Task 6's local helper with the import if one was added.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(production): blank load-file fields whose header line is redacted`.

### Task 10: End-to-end pipeline test

**Files:**
- Test: `backend/tests/test_redaction_pipeline.py`

- [ ] **Step 1: Write the test** — with real `fitz`/Pillow and an in-memory storage sink: ACME email (subject `"RE: ACME settlement"`, body containing `"account 4417-1234"`) → `build_pages` → place a redaction over the subject's header region and one over the account number's word boxes (found from the page-1 sidecar) → `burn_page` on page 1 leaves those pixels black → `produced_text` contains `[REDACTED]`, not `4417`, not `settlement` → `blanked_fields` == `{"subject"}`.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_redaction_pipeline.py` → PASS (it only exercises Tasks 2–9; if it fails, fix the owning task).
- [ ] **Step 3: Commit** `test(redaction): end-to-end imaging → redact → produce pipeline`.

### Task 11: Frontend plumbing

**Files:**
- Modify: `frontend/src/types/index.ts`, `frontend/src/api/client.ts`, `frontend/src/components/ImagePanel.tsx` (cleanup ~lines 58-60)

**Interfaces:**
- Produces types `Redaction {id, document_id, page_num, x_pct, y_pct, w_pct, h_pct, reason_code, note, created_by, created_at, updated_at}`, `RedactionReason` (the 8 codes), `REDACTION_REASON_LABELS: Record<RedactionReason, string>` (Attorney-Client, Work Product, PII, PHI, Confidential, Trade Secret, Non-Responsive, Other), `RedactionQcStatus = 'not_applicable'|'pending'|'approved'|'rejected'`; `DocumentDetail` gains `imaging_status`, `imaging_error`, `imageable`, `image_count`, `redaction_count`.
- Client: `listRedactions(docId)`, `createRedaction(docId, body)`, `updateRedaction(id, body)`, `deleteRedaction(id)`, `decideRedactionQc(docId, decision, note?)`, `getRedactionQcStatus(docId)` (use the production queue endpoint filtered client-side if no per-doc endpoint exists — check `routers/redactions.py`; add `GET /api/documents/{id}/redaction-qc` returning `{status, decision}` in this task if missing), `preparePages(docId)`, `preparePagesBulk(productionId, target)`, `unlockProductionSet(setId)`.
- `fetchImageBlob(docId, page, width, redacted = false)` — appends `&redacted=1`; cache key includes it.
- ImagePanel cleanup must not revoke URLs that `_imageBlobCache` still holds (revoke only on cache eviction).

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Verify** `npm run build` clean; `npx eslint` on touched files adds no errors.
- [ ] **Step 3: Commit** `feat(redaction-ui): client, types, redacted image fetch`.

### Task 12: Prepare-pages in the viewer

**Files:**
- Create: `frontend/src/components/PreparePages.tsx`
- Modify: `frontend/src/components/DocumentViewer.tsx` (center pane selection ~lines 222-271)

**Behaviour:** For an imageless document: `imageable` → button **Prepare pages for redaction**; `queued`/`running` → "Preparing pages…" polling `getDocument` every 3 s (stop on `done`/`failed`, on unmount, or after 10 min → show "Taking longer than expected — Retry"); `failed` → error text + **Retry**; not imageable → "Word, Excel and PowerPoint files can't be redacted yet — withhold or produce in full." For an imaged email/attachment: a **Pages** tab next to the existing Email/Native view; Pages renders `ImagePanel`.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Verify** build + eslint.
- [ ] **Step 3: Commit** `feat(redaction-ui): prepare pages from the viewer`.

### Task 13: Redact mode

**Files:**
- Create: `frontend/src/components/RedactionLayer.tsx` (per-page SVG overlay), `frontend/src/components/RedactionToolbar.tsx`, `frontend/src/hooks/useRedactions.ts`
- Modify: `frontend/src/components/ImagePanel.tsx` (render a `RedactionLayer` in each `#page-N` wrapper when redact mode or boxes exist), `frontend/src/components/DocumentViewer.tsx`

**Interfaces:**
- `useRedactions(docId) -> { redactions, create(box), update(id, patch), remove(id), reason, setReason, selectedId, select(id|null), error }` — optimistic updates with rollback + toast on API error.
- `RedactionLayer` props `{ pageNum, redactions, mode: 'view'|'redact', selectedId, canEdit(r), onCreate, onUpdate, onSelect }`; pointer → percent maths as in `AnnotationOverlay` (`getBoundingClientRect`).

**Behaviour (spec §5.2):** `R` toggles, `Esc` exits; drag to draw (ignore < 0.5 % either way); click selects; drag body moves; corner handles resize; `Delete`/`Backspace` deletes; arrows nudge 0.2 %; all clamped to 0–100; saves on pointer-up; reason picker defaults to last-used (session), initial `attorney_client`; changing reason with a box selected updates it; **Redact page** creates 0,0,100,100 on the page nearest the viewport centre; **Preview as produced** swaps page images to `redacted=true`; unselected boxes translucent black with label; disabled with message "Rotate back to 0° to redact" when rotation ≠ 0; read-only users and non-owners (non-manager) see boxes but handles are hidden.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Verify** build + eslint; manual check in the local dev environment (memory `vigilist-local-dev-env`): draw, move, resize, delete, nudge, page box, preview, rotate-disable, read-only.
- [ ] **Step 3: Commit** `feat(redaction-ui): draw, edit and preview redactions`.

### Task 14: Redactions panel and QC

**Files:**
- Create: `frontend/src/components/RedactionsPanel.tsx`
- Modify: `frontend/src/components/DocumentViewer.tsx` (right sidebar tab "Redactions" shown when the doc has images or redactions)

**Behaviour:** list grouped by page: reason label, author, time, note; click scrolls to `#page-N` and selects the box; QC status badge; managers get **Approve** / **Reject** (optional note) via `decideRedactionQc`, refreshing status.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Verify** build + eslint; manual: approve → status approved; edit a box → status back to pending.
- [ ] **Step 3: Commit** `feat(redaction-ui): redactions list and QC sign-off`.

### Task 15: Production builder — prepare pages, conflicts, unlock

**Files:**
- Modify: `frontend/src/components/ProductionBuilder.tsx` (`CONFLICT_LABELS` ~line 21, validation display, set summary actions)

**Behaviour:** labels — `needs_pages`: "Needs pages prepared (email/PDF/image)", `no_images`: "No page images — Word/Excel/PowerPoint can't be imaged yet", `redactions_overridden`: "Has redactions but set to Produce (would ship unredacted)", `attachment_names_redacted`: "Attachment names are redacted on the parent email, but this attachment ships its filename". When `needs_pages` is non-empty: banner "N documents need pages prepared" + **Prepare pages** (`preparePagesBulk` with those ids), polling validation every 5 s until it clears; lock button disabled while non-empty (even with override). Locked, unpackaged set: **Unlock** with confirm "Unlocking releases the Bates numbers and discards rendered pages." Render 409 drift message shown verbatim.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Verify** build + eslint.
- [ ] **Step 3: Commit** `feat(production-ui): prepare pages before lock, new conflicts, unlock`.

### Task 16: Ship

- [ ] **Step 1:** Full backend suite green; frontend build clean.
- [ ] **Step 2:** Local end-to-end in the dev environment with ACME fake data: an HTML email with an inline image, a multi-page PDF attachment, a scanned PDF, a PNG → prepare pages → redact (covering a subject line and an account number) → QC approve → lock → render → package; open the zip: page image black where boxed, `.txt` shows `[REDACTED]`, DAT `SUBJECT` blank with `REDACTEDFIELDS=SUBJECT`; edit a box after lock → render refuses → unlock → re-lock → render succeeds.
- [ ] **Step 3:** Push branch, open PR (migration `d2l3m4n5o6p7` — note it; check no open PR adds a competing migration).
