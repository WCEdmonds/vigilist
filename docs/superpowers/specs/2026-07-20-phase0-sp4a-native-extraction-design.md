# Phase 0 · Sub-project 4a — Loose-Native Ingest + Python Extraction

**Date:** 2026-07-20
**Status:** Approved design, pending implementation plan
**Roadmap:** `docs/ediscovery-parity-roadmap.md` (Phase 0, SP4 "native/PST processing" — decomposed into SP4a loose-file extraction + SP4b email containers).
**Branch:** `feat/phase0-sp4a-native-extraction`
**Builds on:** SP1 (typed metadata columns, `file_hash_sha256`, `custodian`); the existing `generic_pdf` native path.

## Summary

Let Vigilist ingest a folder of **loose native files** (no load file) of common types and process
them with Python libraries: extract text + metadata, compute SHA-256, and record extraction
exceptions. Today the only no-load-file path is `generic_pdf` (PDF-only, PyMuPDF + Vision OCR).
SP4a adds a `native` ingest mode that handles PDF, modern Office (docx/xlsx/pptx), text, and
images, routing each file to a per-format extractor.

Email containers (PST/MSG/EML) and family/thread **derivation** are **SP4b**. Legacy binary
Office (`.doc/.xls/.ppt`) is treated as unsupported in SP4a.

## Current state (verified)

- `backend/app/services/ingest_pdf.py` — `list_pdf_sources(production_id)` (lists PDFs under
  `productions/{id}/raw/`), `iter_pdf_pages(pdf_bytes, ocr_fn)` (embedded text + Vision OCR
  fallback), `process_pdf_record(production_id, item, global_index, prefix, errors) ->
  Document | None` (renders page JPEGs, builds Document with Bates control number, text,
  image_paths, metadata), `derive_bates_prefix`, `looks_like_bates_stub`.
- `backend/app/services/ingest.py` — `run_ingest_batch(...)` dispatches by `job.source_format`
  (`generic_pdf` → PDF path; else Relativity). `/ingest/process` counts sources per mode and
  fans out Cloud Tasks batches.
- Storage helpers: `get_download_bytes(path)`, `upload_bytes(bytes, path, content_type)`.
- SP1 added Document columns: `file_name`, `file_type`, `source_path`, `custodian`,
  `file_hash_sha256`, `extraction_status`, `extraction_error`.
- `IngestJob.field_mapping` (JSONB, SP1) — per-job config blob.
- Only `pymupdf` is in `backend/requirements.txt` for extraction.

## Design

### 1. Extraction dispatcher — `backend/app/services/extractors.py` (new)

`extract(filename: str, data: bytes, ocr_fn=None) -> ExtractResult` where
`ExtractResult` is a dataclass `{ text: str, file_type: str, extraction_status: str,
extraction_error: str | None }`. Routes by lowercased extension:

| Extension(s) | Extractor | Notes |
|---|---|---|
| `.pdf` | (handled by the PDF path in §3, not here) | dispatcher returns a marker so the caller uses `process_pdf_record` |
| `.docx` | python-docx | paragraphs + table cell text |
| `.xlsx` | openpyxl (read_only, data_only) | non-empty cells joined |
| `.pptx` | python-pptx | text frames across slides |
| `.txt .csv .md .log .json .xml .html .htm .rtf` | decode utf-8 `errors="replace"`, strip nulls | plain text |
| `.jpg .jpeg .png .tif .tiff .gif .bmp` | Vision OCR via `ocr_fn` | image-only |
| `.doc .xls .ppt` (legacy), `.msg .eml .pst` (email), unknown | none | `extraction_status="unsupported"`, empty text |

- Each format extractor is a small function `(_extract_docx(data) -> str`, etc.) wrapped in
  try/except; on failure → `extraction_status="error"`, `extraction_error=str(e)`, empty text.
- Success → `extraction_status="ok"` (or `"partial"` if text is empty but the type is
  supported). `file_type` is a normalized category/extension.
- New pip deps in `backend/requirements.txt`: `python-docx`, `openpyxl`, `python-pptx`.

### 2. Native record processor — `backend/app/services/ingest_native.py` (new)

- `list_native_sources(production_id) -> list[dict]` — every file under
  `productions/{id}/raw/` (like `list_pdf_sources`, unfiltered), each `{storage_path,
  relative_path, filename}`, sorted for stable Bates.
- `process_native_record(db_job_custodian, production_id, item, global_index, prefix, errors)
  -> Document | None`:
  - `control_number = f"{prefix} {global_index + 1:06d}"` (mirrors `process_pdf_record`).
  - Download bytes (`get_download_bytes`); on failure → record error, return None.
  - Compute `file_hash_sha256 = hashlib.sha256(data).hexdigest()`.
  - If PDF extension → delegate to the existing `process_pdf_record` (it renders page images +
    OCR and builds the Document), THEN stamp the SP4a fields on the returned Document
    (`file_hash_sha256`, `file_name`, `file_type`, `source_path`, `custodian`).
  - Else (Office/text/**image**/unsupported) → `extract(filename, data, ocr_fn=_ocr_jpeg)` for
    text (images go through the dispatcher's Vision OCR — text-only, no page render); build the
    Document directly
    with `bates_begin=bates_end=control_number`, `page_count=1`, `text_content`, `file_name`,
    `file_type`, `source_path=relative_path`, `custodian`, `file_hash_sha256`,
    `extraction_status`, `extraction_error`, `metadata_={"File Name": filename, "Folder": ...}`.
  - Never raise out of the per-file processing — bad files become error rows, the batch
    continues.

### 3. Wiring (`ingest.py`, ingest router)

- `run_ingest_batch`: add `elif job.source_format == "native":` → load the batch's source slice
  from `list_native_sources`, read the per-upload custodian from `job.field_mapping.get("custodian")`,
  call `process_native_record` per item, add Documents, update tsvector + counts (mirroring the
  `generic_pdf` branch).
- `/ingest/process`: for `source_format == "native"`, count via `list_native_sources`; persist
  the custodian (from the request body) into the created `IngestJob.field_mapping` under
  `"custodian"`; fan out batches (same batch size as generic_pdf).

### 4. Custodian (per-upload)

The wizard's Custodian field → `/ingest/process` body → stored in
`IngestJob.field_mapping["custodian"]` (no new migration) → `process_native_record` stamps it
on every Document in the upload. Blank custodian is allowed (stays null).

### 5. Frontend (`IngestWizard.tsx`, `api/client.ts`)

- Add a **"Native files"** mode to the wizard's mode selection (alongside Relativity /
  generic-PDF). In native mode, upload all files under the chosen folder to `raw/` (no
  `DATA/*.dat` requirement) and show a **Custodian** text input.
- `startProcessing` / the process call sends `source_format: "native"` and `custodian`.

### 6. Testing

Deterministic unit tests (no network; Vision OCR mocked via `ocr_fn`):
- Per extractor with tiny in-memory fixtures **built in-test using the libs themselves**
  (create a 1-paragraph docx via python-docx, a 1-cell xlsx via openpyxl, a 1-slide pptx via
  python-pptx), asserting extracted text; a `.txt` decode; an unsupported extension → status
  `"unsupported"`; corrupt bytes for a supported type → status `"error"` + non-empty
  `extraction_error`.
- Dispatcher routing table (extension → expected extractor/status), including case-insensitive
  extensions and no-extension files.
- `sha256` of known bytes.
The DB/storage-bound `process_native_record`/wiring is exercised by the app; extractors carry
the unit tests.

## Out of scope (SP4a)
- Email containers `.pst/.msg/.eml` and family/thread **derivation** — **SP4b**.
- Legacy binary Office `.doc/.xls/.ppt` — unsupported here.
- Redaction / production output / OCR-engine changes — later phases.
- No change to the existing `generic_pdf` or Relativity modes.
