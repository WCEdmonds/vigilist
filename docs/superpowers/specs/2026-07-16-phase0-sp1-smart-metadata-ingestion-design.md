# Phase 0 · Sub-project 1 — Smart Metadata Ingestion (load-file path)

**Date:** 2026-07-16
**Status:** Approved design, pending implementation plan
**Roadmap:** `docs/ediscovery-parity-roadmap.md` (Phase 0)
**Branch:** `feat/phase0-sp1-smart-metadata-ingestion`

## Summary

Vigilist ingests already-processed Relativity load files (DAT + OPT) but is **rigid**
(requires an exact `DATA/*.dat` + `*.opt` layout and a fixed set of column names) and
**flattens metadata** (every non-mapped DAT column is dumped into the `metadata_` JSONB rather
than promoted to first-class, queryable fields). This sub-project makes ingest **smart and
metadata-aware** for the load-file path:

1. **First-class typed metadata** — add typed, indexed columns (custodian, dates, hashes, file
   type/name, email headers, extraction status) and populate them.
2. **Smarter parsing** — auto-detect encoding/delimiter/quote and tolerate real-world load-file
   variation instead of demanding one prescribed format.
3. **AI-assisted column mapping with human confirm** — an alias dictionary maps known columns;
   an LLM proposes mappings for leftover columns; the ingest wizard shows a pre-filled
   mapping-review screen the user confirms/overrides before processing.
4. **Backfill** — re-derive the new typed fields for existing documents from their `metadata_`.

This is the first of four Phase-0 sub-projects. Out of scope here (later sub-projects): hash
**dedup logic** (SP2), email **family/threading population** (SP3), and the native/PST
**processing engine** + Tika extraction (SP4 — the "hybrid" native track).

## Current state (verified)

- `backend/app/services/ingest.py` — `ingest_production` (disk) and batch/record helpers.
  `FIELD_MAP` maps only `Begin Bates`/`End Bates`/`Page Count`/`Text Link`/`Native Link` to
  columns; all other DAT columns go to `metadata_` (ingest.py:107–111). Null bytes stripped.
- `backend/app/utils/parsers.py` — `parse_dat` / `parse_opt`.
- Async ingest via Cloud Tasks: `POST /ingest/create` → upload to Firebase Storage
  (`productions/{id}/raw/...`) → `POST /ingest/process` (creates `IngestJob`, counts records,
  enqueues batches) → `POST /ingest/process-batch` (OIDC-verified, builds Documents) →
  `GET /ingest/{job_id}/status`. Modes: `relativity`, `generic_pdf`.
- `frontend/src/components/IngestWizard.tsx` — setup → upload → processing → complete/error.
- `Document` model has `metadata_` (JSONB, column "metadata"), `native_path`, `text_content`,
  and unused `family_id`/`thread_id`/`is_inclusive`. No typed custodian/date/hash/file_type.
- `file_type` is currently *derived* from `native_path` in `services/search.py`
  (`FILE_TYPE_EXTENSIONS`), not stored.

## Design

### 1. Data flow — analyze → confirm → process

Insert an analyze-and-confirm step between upload and processing:

```
/ingest/create → upload files
  → POST /ingest/analyze  → { format, columns: ProposedMapping[], sample_rows }
  → wizard mapping-review screen (user confirms/overrides)
  → POST /ingest/process { field_mapping }   (mapping persisted on IngestJob)
  → /ingest/process-batch applies field_mapping to build typed Documents
  → GET /ingest/{job_id}/status
```

The confirmed `field_mapping` is stored as an explicit object **`{ <canonical_field>:
<source_column_name> }`** (e.g. `{ "custodian": "Cust", "date_sent": "Sent Date" }`), derived
from the confirmed `ProposedMapping[]`. It is persisted in a new `field_mapping` JSONB column
on `IngestJob` so every fanned-out batch applies one consistent interpretation, and it doubles
as the defensibility record of how columns were interpreted.

### 2. Canonical schema + migration

New `Document` columns (Alembic migration):

| Column | Type | Notes |
|--------|------|-------|
| `custodian` | `String` | indexed |
| `date_sent`, `date_received`, `date_created`, `date_modified` | `timestamptz` | stored UTC; `date_sent` indexed |
| `file_hash_md5` | `String(32)` | promoted from load file if present |
| `file_hash_sha256` | `String(64)` | promoted or computed from native; indexed |
| `file_type` | `String(50)` | e.g. extension/category; indexed |
| `file_name` | `String` | original file name |
| `source_path` | `String` | original path in the collection |
| `extraction_status` | `String(20)` | `ok` \| `partial` \| `error`, default `ok` |
| `extraction_error` | `Text` | nullable |
| `email_from` | `String` | |
| `email_to`, `email_cc`, `email_bcc` | `Text` | multi-value, delimiter-preserved |
| `email_subject` | `String` | |

Original raw values for any promoted column are also retained in `metadata_` (nothing lost).
Unmapped columns continue to land in `metadata_`.

Also add a `field_mapping` JSONB column to `IngestJob` (the confirmed
`{canonical_field: source_column}` object from §1), so all batch handlers share one
interpretation.

### 3. Smart parsing — `backend/app/utils/loadfile.py` (new)

- **Encoding detection**: BOM check (UTF-8/UTF-16 LE/BE) then heuristic sniff; fall back to
  Windows-1252. Decode with `errors="replace"`, strip null bytes (as today).
- **Delimiter/quote detection**: recognize Concordance defaults (field `\x14` / `þ` `\xfe`,
  quote `\xfe`), comma, tab, pipe; sniff from the header line. CSV/Excel metadata sheets
  supported.
- **Locate the load file**: accept a `.dat`/`.csv`/`.txt` metadata file not strictly under
  `DATA/`; OPT optional (images may be absent for native-only sets).
- Returns `LoadFileParse { encoding, delimiter, headers: list[str], sample_rows: list[dict],
  total_rows }`. Sample = first N (e.g. 20) rows for mapping/preview.

### 4. Field mapping — `backend/app/services/field_mapping.py` (new)

- **Alias dictionary**: canonical field → known header variants (case/space/underscore
  insensitive). Examples: `bates_begin` ← {BEGDOC, BegBates, Begin Bates, Bates Beg, DocID};
  `custodian` ← {Custodian, Cust}; `date_sent` ← {Date Sent, Sent, DateSent, Sent Date};
  `file_hash_md5` ← {MD5, MD5 Hash, Hash, MD5Hash}; `email_to` ← {To, Email To, Recipients};
  etc. Deterministic, high confidence.
- **AI-assisted**: only columns unmapped by the alias dictionary are sent to the LLM (header
  name + sample values per column) with a strict tool/JSON schema requesting
  `{source_name → canonical_target | null, confidence}`. Alias dict does the bulk; AI handles
  the long tail. Reuses the app's existing Anthropic client pattern; mockable in tests.
- Produces `ProposedMapping[]`: per column `{ source_name, samples, target (canonical|null),
  confidence, source: "alias" | "ai" | "unmapped" }`.

### 5. Normalization — `backend/app/services/metadata_normalize.py` (new)

- **Dates**: parse varied formats (ISO, US `MM/DD/YYYY [HH:MM[:SS]] [AM/PM]`, with/without tz)
  → `datetime` normalized to UTC; keep original string in `metadata_`. Unparseable → leave
  null, record a per-doc note.
- **Hashes**: promote MD5/SHA from the load file when present. Compute `sha256` from the native
  file during batch processing when a native exists (integrity/dedup backbone). *This is the
  one piece with per-document Storage I/O — computed in the batch handler, not the analyze
  step.*
- **Email multi-value**: preserve `To`/`CC`/`BCC` as delimiter-separated text.
- **file_type**: derive from `file_name`/`native_path` extension (reuse `FILE_TYPE_EXTENSIONS`)
  or an explicit File Type/Extension column if mapped.

### 6. Ingest wizard confirm UI — `IngestWizard.tsx`

A new `mapping` stage between `uploading` and `processing`:
- Table of detected columns: `source_name`, sample values, a **target-field dropdown**
  pre-filled from the proposed mapping, confidence, and a source badge —
  **alias = auto/green, ai = review/amber, unmapped = grey**.
- Canonical targets listed (Bates, custodian, dates, hashes, email fields, "leave in
  metadata", "ignore").
- Clean Relativity files: one-click confirm. Messy files: corrected here.
- Confirming calls `/ingest/process` with the `field_mapping`.

### 7. Backfill migration

A data migration re-derives the new typed fields for existing documents from their `metadata_`
JSONB using the **alias dictionary only** (deterministic, no AI, no network). Idempotent and
batched (won't overwrite a value already set). Dates normalized via the same normalizer.

### 8. Error handling

- Per-document `extraction_status` / `extraction_error`; malformed rows and unreadable natives
  recorded and surfaced in the existing `IngestJob` error list rather than silently dropped.
- Analyze step degrades gracefully: if the LLM is unavailable, unmapped columns simply stay
  `unmapped` (user maps them manually) — ingest is never blocked on AI.

### 9. Testing

Deterministic unit tests (no DB / no network, following `backend/tests/test_org_access.py`
patterns), covering:
- Encoding + delimiter/quote detection across fixtures (Concordance `þ`, CSV, tab, UTF‑16,
  BOM/no-BOM).
- Alias matching (case/space/underscore variants; ambiguous headers).
- Date/timezone normalization (ISO, US formats, AM/PM, tz offsets, unparseable).
- Hash promotion + `file_type` derivation.
- Backfill re-derivation from a `metadata_` fixture.
- AI mapping path with a mocked client (schema-valid response; unavailable-client fallback).

Fixtures: a `backend/tests/fixtures/loadfiles/` set of small real-world load-file variants.

## Out of scope (SP1)
- Hash **dedup** (SP2), email **family/threading population** (SP3), native/PST **processing
  engine** + Tika extraction (SP4).
- No change to the `generic_pdf` ingest mode beyond sharing the new metadata columns where
  trivially applicable.
