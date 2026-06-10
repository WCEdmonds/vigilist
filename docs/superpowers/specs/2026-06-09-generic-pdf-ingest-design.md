# Generic PDF Folder Ingest — Design

**Date:** 2026-06-09
**Author:** Will Edmonds / Claude Code
**Status:** Approved design, pending implementation plan

## Problem

The ingest pipeline only accepts Relativity-format productions. The frontend
wizard rejects any folder without a `DATA/*.dat` file, and the backend
(`bootstrap_ingest_source`) raises `FileNotFoundError` without both a `.dat`
and `.opt` file. Everything keys off Bates numbers parsed from those files.

Users need to ingest non-standard discovery folders — specifically, a folder
full of PDFs organized into arbitrary subfolders — that have no load files and
no Bates numbers.

## Scope

In scope (first version):

- **PDFs only.** Born-digital and scanned PDFs.
- Each PDF becomes one `Document`.
- Subfolder structure preserved as searchable metadata.
- Coexists with the existing Relativity pipeline via an explicit, auto-suggested
  mode toggle in the upload wizard.

Out of scope (future work): loose image files, Office documents, email
(`.msg`/`.eml`), threading/families.

## Approach

**Parallel generic pipeline that reuses the existing job machinery.** Keep one
upload flow, one `IngestJob`, one Cloud Tasks fan-out, one status poller. Add a
`source_format` flag to the job; the batch worker branches on it. For
Relativity, behavior is unchanged. For generic PDF, "a record" becomes "a PDF
file," and turning it into a `Document` is isolated to one new function.

Rejected alternatives:

- **Synthesize a fake DAT/OPT and reuse the existing parser** — hacky two-pass
  process; PDFs map poorly onto the per-page-TIFF + OPT model.
- **Fully separate `/ingest/pdf/*` endpoints and worker** — duplicates the
  Cloud Tasks fan-out and idempotency logic for no real benefit.

## Document mapping for a PDF

| Field | Value |
|-------|-------|
| `bates_begin` / `bates_end` | Synthetic control number, e.g. `SMITH 000001` |
| `title` | The PDF's filename (stem) |
| `metadata_` | `{"File Name": "<name>.pdf", "Folder": "<relative subfolder path>"}` |
| `page_count` | Number of PDF pages |
| `image_paths` | One JPEG per page, rendered at **250 DPI** via PyMuPDF, stored under `productions/{id}/converted/` |
| `text_content` | Embedded text layer per page; OCR fallback (Cloud Vision) for sparse/scanned pages |
| `native_path` | The original PDF in `productions/{id}/raw/<relative path>` (stays downloadable) |
| `processing_status` | `complete` once processed |

### Control number assignment

- **Prefix** is auto-derived from the production name (no manual entry):
  uppercase the name, strip characters other than `A-Z`/`0-9`/space, collapse
  whitespace, take the first whitespace-delimited token, truncate to 12
  characters; fall back to `DOC` if the result is empty. (E.g. "Smith Loose
  Docs" → `SMITH`.)
- **Number** is the file's 1-based position in the deterministically sorted
  list of uploaded PDFs, zero-padded to 6 digits: `{PREFIX} {index:06d}`.
- Because the number is derived from the sorted index, a retried batch produces
  the **same** control numbers, so the existing skip-by-`bates_begin`
  idempotency holds without change.

### Text extraction

- Use PyMuPDF's embedded text layer per page (`page.get_text()`).
- If a page's extracted text is empty or below a small threshold (under ~10
  non-whitespace characters — a scanned page), render that page to JPEG and OCR
  it via the existing `ocr_image_vision_bytes` Cloud Vision path.
- Born-digital PDFs skip OCR entirely, saving Vision cost.

## Backend flow

Mirrors the existing flow:

1. `/ingest/create` — unchanged (creates the production, syncs Firebase claims).
2. Frontend uploads only the `.pdf` files to
   `productions/{id}/raw/<relative path>`.
3. `/ingest/process` — branches on `source_format`:
   - For `generic_pdf`: list the uploaded PDFs in storage to get `total_files`,
     create the `IngestJob` with `source_format='generic_pdf'`, fan out batches
     (Cloud Tasks, or inline `BackgroundTask` fallback) exactly as today.
4. `/ingest/process-batch` → `ingest_batch` reads `job.source_format` and
   dispatches each slice item to either today's `process_ingest_record` or the
   new `process_pdf_record`. Shared job-progress bookkeeping (processed/skipped/
   errors counters, finalize) is factored into helpers rather than duplicated.

### Source listing for generic PDF

A new `list_pdf_sources(production_id) -> list[dict]` lists `*.pdf` files under
`productions/{id}/raw/`, sorted deterministically by storage path. Each item
carries the remote storage path and the relative subfolder path. This is the
generic-PDF analog of `bootstrap_ingest_source`, and slice indices `[start:end]`
work identically.

## Data model change

- Add nullable `source_format` column to `IngestJob`
  (`String(20)`, default `'relativity'`).
- One Alembic migration.
- **No change to `Document`** — the synthetic control number satisfies the
  non-null `bates_begin`/`bates_end` and the `(production_id, bates_begin)`
  unique key, so search, tagging, notes, and the viewer all work unchanged.

## Frontend change

`IngestWizard` gets a mode toggle: **"Relativity production"** vs **"Folder of
files (PDFs)."**

- On folder select, auto-detect: `DATA/*.dat` present → suggest Relativity;
  otherwise → suggest PDF. Pre-select the detected mode.
- Warn if the user's selected mode mismatches the folder contents (e.g. chose
  Relativity but no `.dat` found).
- PDF mode validates that at least one `.pdf` is present and uploads **only**
  the PDF files (not every file in the folder).
- Pass `source_format` to `/ingest/process`.

API client (`startProcessing` and/or `createProductionForIngest`) accepts and
forwards `source_format`.

## Error handling

- A PDF that fails to open or render is logged to `job.errors` and counted as
  skipped — same semantics as a bad DAT row today. The job still completes.
- Batch size is smaller for PDFs than the Relativity default of 25 (a single
  file can be many pages): **10 PDFs per batch**.

## AI titles

`title` is set to the filename, so the existing finalize step (which only
titles documents with `title IS NULL`) naturally skips these documents. This
matches the chosen "filename as title" behavior.

## New dependency

- `pymupdf` added to `backend/requirements.txt` — pure-Python, renders pages
  and extracts text, no system binaries (poppler) required for Cloud Run.

## Testing

- `process_pdf_record`:
  - born-digital PDF → text from embedded layer, OCR not invoked
  - scanned/image-only PDF → OCR fallback invoked
  - multi-page PDF → one image per page
  - control-number determinism: same index → same `bates_begin` across calls
- Wizard auto-detect logic: folder with `DATA/*.dat` suggests Relativity;
  folder of PDFs suggests generic; mismatch warning fires.
