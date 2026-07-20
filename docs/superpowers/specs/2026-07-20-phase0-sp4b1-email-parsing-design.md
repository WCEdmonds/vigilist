# Phase 0 · Sub-project 4b-1 — Email Container Parsing + Family

**Date:** 2026-07-20
**Status:** Approved design, pending implementation plan
**Roadmap:** `docs/ediscovery-parity-roadmap.md` (Phase 0, SP4 native/PST — SP4b decomposed into 4b-1 email→Documents+family+PST, and 4b-2 thread/inclusive derivation).
**Branch:** `feat/phase0-sp4b1-email-parsing`
**Builds on:** SP1 (email_* + typed columns), SP3 (family_id + Family panel), SP4a (native ingest + extractor dispatcher).

## Summary

Ingest email containers as part of the native pipeline: parse `.eml`/`.msg` and explode `.pst`/
`.ost` (via the `readpst` CLI) into individual messages, turning each message into a **parent**
Document (email headers → SP1 `email_*` fields, body → text) and each **attachment** into a
separate **family-member** Document sharing the parent's `family_id`. This lights up SP3's
Family panel for real emails and fills the email metadata fields from parsed content.

Today SP4a's extractor marks `.msg/.eml/.pst` as `unsupported`. SP4b-1 makes native ingest
expand these one source file into MANY Documents (message + attachments; a PST → all its
messages), which the current one-file→one-Document batch does not do.

**Thread and `is_inclusive` derivation are SP4b-2** (a production-wide post-pass) and out of
scope here.

## Current state (verified)

- SP4a: `services/extractors.py` `extract(filename, data, ocr_fn=None) -> ExtractResult`
  (`.msg/.eml/.pst` → `unsupported`); `services/ingest_native.py`
  `process_native_record(custodian, production_id, item, global_index, prefix, errors) ->
  Document | None`, `ingest_native_batch(...)`; `list_native_sources`.
- `ingest.py` batch loop (`ingest_native_batch` mirrors `ingest_pdf_batch`): `for global_index,
  item in slice_pairs: doc = process_native_record(...); _persist_document(db, job_id, doc)` —
  **one item → one Document**.
- SP1 Document columns: `email_from/to/cc/bcc/subject`, `date_sent`, `custodian`,
  `file_hash_sha256`, `file_name`, `file_type`, `source_path`, `extraction_status`/`error`.
- SP3: `family_id` column + `GET /documents/{id}/family` panel showing family members.
- `backend/Dockerfile` — `python:3.13-slim`; an `apt-get install` RUN at line 6; then
  `COPY requirements.txt` + `pip install -r requirements.txt`.
- Storage helpers `get_download_bytes`, `upload_bytes`; `_ocr_jpeg` in `ingest_pdf.py`.

## Design

### 1. Email expander — `backend/app/services/email_parse.py` (new)

`@dataclass ParsedMessage { from_: str, to: str, cc: str, bcc: str, subject: str,
date_sent: str | None, body_text: str, attachments: list[tuple[str, bytes]] }` and:

`expand_email(filename: str, data: bytes) -> list[ParsedMessage]`:
- `.eml` → stdlib `email.message_from_bytes` → one `ParsedMessage` (walk parts for body text +
  attachments).
- `.msg` → `extract-msg` (`extract_msg.Message` from bytes/temp) → one `ParsedMessage`.
- `.pst`/`.ost` → write bytes to a temp file, shell out `readpst -e -o <tmpdir> <pst>` (one
  `.eml` per message), then parse each produced `.eml` via the stdlib path → N `ParsedMessage`.
  Temp dir removed in `finally`.
- Never raises: on parse/readpst failure, return `[]` (the caller records an error row). A
  helper `_parse_eml_bytes(data) -> ParsedMessage` is the shared, unit-testable core.

`extract-msg` added to `requirements.txt`; `pst-utils` (readpst) added to the Dockerfile
`apt-get` line.

### 2. One-file→many in the native batch (`ingest_native.py`, `ingest.py`)

- Add `process_native_email(custodian, production_id, item, global_index, prefix, errors) ->
  list[Document]`: `expand_email(filename, data)`; for each `ParsedMessage`, create a **parent**
  Document — `bates_begin=bates_end=control_number`, `family_id=control_number`,
  `email_from/to/cc/bcc/subject` from headers, `date_sent=normalize_date(date_sent)`,
  `text_content=body_text`, `file_name`, `file_type` (`email`), `source_path`, `custodian`,
  `file_hash_sha256 = sha256(the message's serialized .eml/.msg bytes)`, `extraction_status`.
  For each attachment, create a **child** Document with `family_id = <parent control number>`,
  `text_content` from SP4a's `extract(attachment_name, att_bytes, ocr_fn=_ocr_jpeg)`,
  `file_hash_sha256 = sha256(att_bytes)`, `file_name = attachment_name`, and the same custodian.
  **Control-number scheme (deterministic, so retried batches reproduce them):** the message =
  `f"{prefix} {global_index+1:06d}"` (e.g. `PREFIX 000123`); its Kth attachment (1-based) =
  `f"{message_control} .{k:04d}"` (e.g. `PREFIX 000123 .0001`). Message-level uniqueness across
  the container comes from the container being one source `global_index`; when a PST yields many
  messages, disambiguate messages within the container by appending a message sub-index to the
  control number (`f"{prefix} {global_index+1:06d} -{m:04d}"`) before the attachment suffix, so
  every message and attachment across the whole PST is unique and reproducible.
- In `ingest_native_batch`: if the item's extension is an email/PST type, call
  `process_native_email` and persist EACH returned Document (`_persist_document` per doc);
  otherwise the existing single-Document `process_native_record` path. Wrap per-item in the
  existing try/except → error row + skip; never abort the batch.

### 3. Infra

- Dockerfile: add `pst-utils` to the existing `apt-get install -y --no-install-recommends`
  list (provides `readpst`). `requirements.txt`: add `extract-msg`.
- `readpst` invoked via `subprocess.run([...], check=True, timeout=...)` into a `tempfile`
  directory; the temp dir is always cleaned up; a non-zero exit / timeout / missing binary is
  caught and yields `[]` (error row), never an exception out of the batch.

### 4. Scaling caveat (documented, not silent)

A very large PST is exploded and parsed within a single batch item (one Cloud Task), bounded by
that task's time/memory. This is acceptable for v1; a later optimization can pre-explode PSTs
into per-message native sources for finer-grained batching. Recorded as a known limit in code
comments + the ingest job when a container is large.

### 5. Testing

Deterministic unit tests (no network; `readpst` NOT invoked in unit tests):
- `_parse_eml_bytes`: build a small multipart `.eml` in-test via stdlib `email.message.EmailMessage`
  (a From/To/Subject/Date, a text body, and one attachment) → assert headers, body, and one
  attachment `(name, bytes)` are extracted.
- `.msg`: build a minimal message via `extract-msg`'s round-trip if feasible, else parse a tiny
  committed `.msg` fixture; assert headers + body.
- Family linking: `process_native_email`'s pure structuring (given a `ParsedMessage` with an
  attachment, the parent + child Documents share `family_id`, distinct control numbers) — test
  the pure helper that builds the Documents (dependency-inject the extract/hash so no storage).
- `.pst` path is integration-only: `expand_email` for `.pst` shells out to `readpst`; in unit
  tests, either skip when the binary is absent or inject a fake exploder. Do NOT require
  `readpst` in the test environment.

## Out of scope (SP4b-1)
- Thread + `is_inclusive` derivation — **SP4b-2** (production-wide post-pass).
- Calendar/contact/task items inside PSTs (messages only).
- Encrypted/password-protected PSTs (→ error row).
- Pre-explode/finer batching of very large PSTs (documented caveat).
