# Phase 0 · Sub-project 2 — Hash-based Deduplication

**Date:** 2026-07-17
**Status:** Approved design, pending implementation plan
**Roadmap:** `docs/ediscovery-parity-roadmap.md` (Phase 0)
**Branch:** `feat/phase0-sp2-hash-dedup`
**Builds on:** SP1 (added `Document.file_hash_sha256`, `Document.custodian`).

## Summary

Add **true byte-identical deduplication** using the `file_hash_sha256` column SP1 populates,
integrated into the existing duplicate-detection infrastructure. Today's `detect_duplicates`
has a **misnamed "exact" tier**: it is MinHash text-similarity ≥ 0.95 and only considers
documents with extracted text (`text_content is not null`) — so it calls 95%-similar text
"exact" and **completely misses byte-identical files that have no text** (image-only docs,
un-OCR'd natives). SP2 adds a genuine byte-identical tier and records the custodians who held
each duplicate (the standard e-discovery custodian rollup).

**Behavior (v1): identify + custodian rollup — non-destructive.** Group byte-identical docs,
surface them in the existing duplicates UI as the highest-confidence tier, and show which
custodians held each. It does NOT suppress duplicates from review (deferred to a later SP).

## Current state (verified)

- `backend/app/services/duplicates.py` — `detect_duplicates(db, production_id)` clears all
  `DuplicateGroup`/`DocumentDuplicate` rows for the production, then runs two passes over
  documents **with text**: MinHash Jaccard ≥ 0.95 → `DuplicateGroup(type="exact")`; embedding
  cosine 0.80–0.95 → `type="similar"`. Early-returns if `len(text_docs) < 2`. Returns
  `{exact_groups, similar_groups, total_documents_grouped}`.
- `backend/app/routers/intelligence.py` — `POST /productions/{id}/detect-duplicates`
  (manager+), and `GET /documents/{id}/duplicates` → `list[DuplicateEntryOut]`.
- `backend/app/schemas.py` — `DuplicateEntryOut(document_id, bates_begin, title, similarity, type)`.
- Models: `DuplicateGroup(id, production_id, type)`, `DocumentDuplicate(document_id, group_id, similarity)`.
- Frontend: `CorpusAnalysis.tsx` triggers `detectDuplicates(productionId)`;
  `DocumentViewer.tsx` renders per-document duplicates via `getDocumentDuplicates`.

## Design

### 1. Backend — byte-identical grouping pass (`services/duplicates.py`)

Add a third pass to `detect_duplicates`, run **independently of text**:
- Query all documents in the production with a non-null, non-empty `file_hash_sha256`
  (`select id, file_hash_sha256 where production_id = :pid and file_hash_sha256 is not null`).
- Group by hash (a plain dict). For each hash shared by **≥ 2** documents, create one
  `DuplicateGroup(type="hash")` and a `DocumentDuplicate(document_id, group_id, similarity=1.0)`
  per member.
- This pass must run **before** the existing `if len(text_docs) < 2: return` guard — that
  guard is about the text passes only; byte-identical image dupes have no text and must not be
  gated by the text-doc count. Restructure so the hash pass always runs; the MinHash/embedding
  passes keep their text-based guard.
- The pass is extracted into a **pure, testable helper** — e.g.
  `group_by_hash(rows: list[tuple[str, str]]) -> list[list[str]]` (input `(doc_id, sha256)`,
  output list of doc-id groups, size ≥ 2) — so the grouping logic is unit-tested without a DB;
  `detect_duplicates` calls it and persists the groups.
- `detect_duplicates` return dict gains `hash_groups: int`;
  `total_documents_grouped` includes hash-group members.

### 2. Custodian rollup (derived — no schema change)

- `DuplicateEntryOut` gains `custodian: str | None`.
- `GET /documents/{id}/duplicates` includes each duplicate's `custodian`, read from the
  `Document.custodian` column (SP1) via the existing join in `get_document_duplicates`.
- The UI derives the group's custodian rollup from these values ("also held by: …"). No
  denormalized column is added.

### 3. Frontend — labels + custodian (`DocumentViewer` duplicates panel, `api/client.ts`)

- Relabel tiers honestly in the duplicates panel: `hash` → **"Identical file"**, `exact` →
  **"Near-identical text"**, `similar` → **"Similar"**.
- Sort `hash` first (highest confidence), then `exact`, then `similar`.
- Show each duplicate's `custodian` (when present) next to its Bates/title.
- Add the `custodian` field to the frontend duplicate type consuming `getDocumentDuplicates`.

### 4. Error handling / edge cases

- Documents with no `file_hash_sha256` (e.g. image-only sets with no native, or pre-SP1 docs
  whose native wasn't hashed) simply don't participate — no error.
- A document may legitimately appear in BOTH a `hash` group and a text (`exact`/`similar`)
  group; that is allowed and the UI lists each. No cross-pass exclusion in v1.
- Re-running `detect_duplicates` clears and rebuilds all groups including `hash` (idempotent).

### 5. Testing

Deterministic unit tests (no DB / no network, following `backend/tests/` conventions):
- `group_by_hash`: two docs same hash → one group; three docs, two share a hash → one group of
  2 + singleton excluded; all-distinct hashes → no groups; null/empty hash excluded; a group of
  ≥ 3 identical.
- Custodian rollup: `get_document_duplicates` returns each entry's custodian (helper/logic
  tested at the pure level where feasible).
- Confirm the hash pass is not gated by the text-doc-count guard (structural test or a targeted
  assertion).

## Out of scope (SP2)
- Suppression of duplicates from review (queues/batches/sampling) — a later sub-project.
- Cross-production/global-corpus dedup (this is per-production, matching existing behavior).
- MD5-based grouping (SHA-256 is the dedup key; MD5 remains a stored attribute only).
- Any change to the MinHash/embedding tiers beyond the UI relabel.
