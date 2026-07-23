# P0-SP5 — Document Source Designation + Workspace Mode

**Date:** 2026-07-22
**Phase:** 0 follow-up (data foundation), feeds P2-4
**Depends on:** stacked on P2-3.5 (PR #41) — the new validation conflict extends that service
**Consumed by:** P2-4 (builder source filter, outgoing-mode panels), everyday review

## Decision context (2026-07-22)

- A matter holds both directions of a case: our collection AND productions
  received from other parties. Today nothing separates them except Bates
  prefixes and ad-hoc metadata — reviewers see their own documents mixed
  into the opposing production and vice versa, and nothing stops a received
  document from landing in an outbound production set.
- **Model mirrors `custodian` end-to-end** (the codebase's existing
  labeled-at-ingest, typed, indexed per-doc string): two new Document
  columns set per ingest load, threaded through the ingest job the same way
  custodian is (via `field_mapping` JSONB), stamped at every
  `Document(...)` construction site.
- **Workspace mode is a toggle, not a login gate** (user direction): an
  Incoming / Outgoing / All segmented control in the matter header,
  persisted per user+matter in localStorage. It presets the `source_type`
  filter on search/browse. Panel re-emphasis (builder, privilege log, QC
  queue in outgoing mode) ships with P2-4 — the toggle is a default,
  never a wall.
- Received documents in an outbound set become the **fourth validation
  conflict** (`received_document`) — override-able like the others, since
  protocols occasionally require re-producing received material.

## 1. Data model (one migration, import-safe)

- `documents.source_party` — String(255), nullable, indexed
  (`ix_documents_source_party`). Free label: "Our Collection",
  "ABC Corp Production"…
- `documents.source_type` — String(20), nullable —
  `collection` | `received`. NULL = undesignated (legacy loads).
- No IngestJob columns: `source_party`/`source_type` ride in
  `IngestJob.field_mapping` JSONB exactly like `custodian` does.
- `down_revision = "d0e1f2a3b4c5"`.

## 2. Ingest propagation

- `POST /api/ingest/process` accepts optional `source_party: str` and
  `source_type: "collection"|"received"` in the body; both are folded into
  `field_mapping` beside `custodian` (both Cloud-Tasks and inline paths).
  Invalid `source_type` → 422.
- Every `Document(...)` construction site that stamps `custodian` also
  stamps `source_party`/`source_type` from the job: `ingest.py`
  (`process_ingest_record`, legacy `ingest_production`),
  `ingest_native.py` (email parent, attachments, native records, the
  post-`process_pdf_record` stamping block), `ingest_pdf.py` if it stamps
  custodian.
- `source_party` joins `_METADATA_TARGETS` in `metadata_normalize.py` so a
  load-file column can also populate it per-document; the job-level value
  is the fallback when the column is absent. `source_type` is job-level
  only (a load is either received or collected — not per-document).

## 3. Search / browse filtering

- `GET /api/search` and `GET /api/documents` gain optional `source_party`
  and `source_type` query params, applied as typed-column conditions
  (exact match on `source_type`, exact on `source_party`).
- New `GET /api/productions/{production_id}/source-parties` → distinct
  non-null `source_party` values (+ whether any docs are undesignated),
  for the dropdown. Access-checked like other production reads.

## 4. Production builder integration (backend now, UI in P2-4)

- `POST /production-sets/{id}/documents` gains
  `exclude_received: bool = False`: drops candidates with
  `source_type == "received"` unless explicitly listed in `document_ids`
  (same explicit-wins rule as the duplicate filter). Response gains
  `skipped_received` count.
- `compute_conflicts` gains category `received_document`: any member with
  `source_type == "received"` (detail names the source party). Lock's
  conflict gate and override flow apply unchanged.

## 5. Frontend

- **IngestWizard**: a "Document source" block for all modes — segmented
  choice *Our collection* / *Received production* (maps to `source_type`;
  default *Our collection*) + a party label input (`source_party`,
  placeholder differs by choice: "Our Collection" / "ABC Corp").
  Passed through `startProcessing` → `/api/ingest/process`.
- **Search/browse**: a source dropdown beside the existing file-type
  dropdown in `App.tsx` (options from the source-parties endpoint, plus
  "All sources"); wired into `searchDocuments`/`listDocuments` as
  `source_party`.
- **Mode toggle**: Incoming / Outgoing / All segmented control in the
  matter header. Persisted `localStorage["vigilist:mode:{productionId}"]`,
  default All. Incoming → `source_type=received` on search/browse;
  Outgoing → `source_type=collection`; All → no param. Never blocks
  navigation or hides documents a direct link reaches.

## 6. Testing

- Backend fake-session tests: ingest process endpoint folds fields into
  field_mapping (+422 on bad source_type); search/documents filters apply
  (assert SQL contains the column conditions via the fake's captured
  statements); source-parties endpoint; add-documents `exclude_received`
  (+ explicit-id exception, `skipped_received` count); validation
  `received_document` conflict (+ NULL source_type not flagged).
- Full backend suite green; frontend `npm run build` (tsc) passes.

## Out of scope

- Backfilling existing documents (manual/SQL when needed; NULL renders as
  "undesignated" everywhere and is never treated as received).
- Outgoing-mode panel re-emphasis and builder UI — P2-4.
- Per-document source_type from load-file columns.
