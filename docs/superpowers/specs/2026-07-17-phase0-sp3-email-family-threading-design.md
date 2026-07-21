# Phase 0 · Sub-project 3 — Email Family & Threading

**Date:** 2026-07-17
**Status:** Approved design, pending implementation plan
**Roadmap:** `docs/ediscovery-parity-roadmap.md` (Phase 0)
**Branch:** `feat/phase0-sp3-email-family-threading`
**Builds on:** SP1 (field-mapping + promotion + backfill pattern).

## Summary

Populate the three `Document` columns that exist but are never filled —
`family_id`, `thread_id`, `is_inclusive` — from load-file columns, and use them: a
Family/Thread panel in the document viewer and the already-stubbed tag-propagation paths.

- `family_id` groups a parent email with its attachments (an email family).
- `thread_id` groups emails in the same conversation.
- `is_inclusive` marks the most-complete message in a thread (email-threading concept), stored
  as a boolean.

**No schema migration** — the three columns already exist on `Document` (original model). SP3
adds the mapping, a data backfill, an endpoint, the propagate-tag branches, and the UI.

Deriving families/threads by parsing email containers (when the load file lacks these columns)
is **SP4**'s domain (native/PST processing) and is out of scope here.

## Current state (verified)

- `Document` has `family_id` (String, nullable), `thread_id` (String, nullable), `is_inclusive`
  (Boolean, not-null default False) — all currently unpopulated.
- `backend/app/services/field_mapping.py` `CANONICAL_FIELDS`/`ALIAS_DICT` do **not** include
  family/thread/inclusive.
- `backend/app/services/metadata_normalize.py` — `promote_record` classifies canonical targets
  via `_METADATA_TARGETS` (typed columns), `_STRUCTURAL_TARGETS`, `_DATE_TARGETS`. No boolean
  handling yet.
- `backend/app/routers/intelligence.py` `propagate_tag` accepts
  `relationship_type: Literal["duplicate", "family", "thread"]` (`schemas.py:486`) but only the
  `"duplicate"` branch is implemented; family/thread are dead because the fields are empty.
- `GET /documents/{id}/duplicates` → `DuplicateEntryOut` powers the DocumentViewer duplicates
  panel — the model to mirror for a family/thread panel.
- SP1 established: alias-only, idempotent, per-batch-committed **data backfill** migration that
  re-derives typed fields from the `metadata_` JSONB.

## Design

### 1. Mapping + promotion (`field_mapping.py`, `metadata_normalize.py`)

- Add canonical fields `family_id`, `thread_id`, `is_inclusive` to `CANONICAL_FIELDS`, with
  `ALIAS_DICT` entries:
  - `family_id` ← Group Identifier, GroupID, Group ID, Family Range, Family ID, Family,
    Parent Doc ID, Parent ID, Attachment Parent
  - `thread_id` ← Thread ID, ThreadID, Conversation Index, Conversation ID, Email Thread
  - `is_inclusive` ← Inclusive Email, Email Inclusive, Inclusive, Is Inclusive
- In `metadata_normalize.py`:
  - Add `family_id`, `thread_id`, `is_inclusive` to `_METADATA_TARGETS`.
  - Add `_BOOL_TARGETS = {"is_inclusive"}` and `normalize_bool(value: str) -> bool | None`
    (True for `yes`/`y`/`true`/`t`/`1` case-insensitively; False for `no`/`n`/`false`/`f`/`0`;
    None for empty/unrecognized).
  - In `promote_record`, route bool targets through `normalize_bool`, date targets through
    `normalize_date` (unchanged), everything else as string pass-through. `family_id`/`thread_id`
    are strings.
- New ingests then populate all three via the existing `_apply_metadata` promotion (SP1
  Task 6) with no further wiring.

### 2. Backfill (data migration, no schema change)

An alias-only, idempotent, per-batch-committed data migration (mirroring SP1's
`n6b1c3d95e02`) that, for existing documents, re-derives `family_id`/`thread_id`/`is_inclusive`
from the original load-file columns preserved in `metadata_` — using `match_aliases` +
`promote_record`. Idempotency via a NULL guard (`family_id`/`thread_id`) and, for the
not-null-default `is_inclusive`, only set it when a recognized inclusive column is present and
the current value is the default `False`. Runs inside an `autocommit_block` with LIMIT/OFFSET
batching (as SP1). This is the one prod-touching piece; verify upgrade+downgrade against real
Postgres before merge.

### 3. Family/Thread endpoint + propagate-tag (`routers/intelligence.py`, `schemas.py`)

- `GET /documents/{id}/family` → `FamilyThreadOut { family: list[FamilyMemberOut], thread:
  list[FamilyMemberOut] }` where `FamilyMemberOut { document_id, bates_begin, title,
  is_inclusive }`. `family` = other docs with the same non-null `family_id`; `thread` = other
  docs with the same non-null `thread_id`; both access-scoped to the user's accessible
  productions and excluding the doc itself, ordered by `bates_begin`. If the doc has no
  `family_id`/`thread_id`, that list is empty.
- Implement `propagate_tag`'s `"family"` and `"thread"` branches: `related_ids` = docs (in the
  same production, excluding self) sharing this doc's `family_id` / `thread_id` (only when that
  id is non-null). The existing tag-apply + audit-log loop is unchanged.

### 4. Frontend — Family/Thread panel (`DocumentViewer.tsx`, `api/client.ts`, `types/index.ts`)

- `getDocumentFamily(docId)` client fn returning `{ family: FamilyMember[]; thread:
  FamilyMember[] }`; `FamilyMember { document_id, bates_begin, title, is_inclusive }` type.
- A panel below/near the duplicates panel: two labeled groups — **Family** (parent email +
  attachments) and **Thread** — each a clickable list (navigate on click, like duplicates),
  with an **"Inclusive"** badge on `is_inclusive` members. Render each group's header with its
  count; hide a group when empty. Fetched on `docId` change alongside the existing
  duplicates/annotations fetches.

### 5. Testing

Deterministic unit tests (no DB/network, `backend/tests/` conventions):
- `normalize_bool`: `Yes`/`Y`/`true`/`t`/`1` → True; `No`/`N`/`false`/`0` → False; ``/`maybe`
  → None; non-string → None.
- Alias matching: family/thread/inclusive header variants resolve to the right canonical.
- `promote_record`: emits `family_id`/`thread_id` as strings and `is_inclusive` as a bool from
  a record + mapping; unmapped leftovers preserved.
- Backfill re-derivation from a `metadata_` fixture (the pure helper level, as SP1).
- Frontend: `npm run build` + lint clean; manual check of the panel.

## Out of scope (SP3)
- Deriving family/thread/inclusive from parsed email containers/headers when the load file
  lacks them — **SP4** (native/PST processing).
- Dedicated date-ordered conversation view, inclusive-only review filter, thread suppression
  from review — deferred (the "full threading" option not chosen).
