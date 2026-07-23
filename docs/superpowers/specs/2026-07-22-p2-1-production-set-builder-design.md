# P2-1 — Production Set Builder + Bates Numbering

**Date:** 2026-07-22
**Phase:** 2 (Production / Deliverable Output), sub-project P2-1
**Depends on:** Phase 0 (metadata, families, hash dedup), Phase 1 (redactions, privilege dispositions — all merged)
**Consumed by:** P2-2 (endorsement/slip-sheets/rendering), P2-3 (DAT/OPT + manifest + packaging), P2-4 (builder UI)

## Decision context (brainstormed 2026-07-22)

- **"Production set" is a NEW entity.** The existing `Production` model is the
  matter/case container, not a deliverable. Phase 2 introduces `ProductionSet`
  (a deliverable volume built within a matter) — the name collision is
  unfortunate but grandfathered; code and docs must not conflate them.
- **Fresh Bates per production set.** `Document.bates_begin/end` is an ingest
  control number (imported Bates from received productions, or generated
  `message_control`/`att_control` for native ingest) — NOT numbers we stamped.
  Production numbers are assigned at production time from the set's own
  prefix, stored on the set membership, never on the document. The same doc
  can appear in multiple sets with different numbers.
- **Draft → lock lifecycle, number at lock.** Membership is freely editable
  in draft with no numbers assigned. `POST /lock` orders members, assigns
  gap-free sequential Bates in one transaction, and freezes the set.
  Locked sets are immutable (no unlock) — numbers never shift or gap, which
  is the defensibility requirement. Rejected alternatives: number-on-add
  (removals create permanent gaps, ordering frozen at insertion order) and
  virtual sets/saved queries (membership drifts with tag edits; a Bates
  number must permanently identify one document).
- **Snapshot at lock.** Each member's `pages` and `disposition` are copied
  onto the membership row at lock time so P2-2 renders exactly what was
  numbered even if redactions/tags/overrides change later. The locked set is
  the record of what was produced.
- **PDF first, TIFF later** (affects P2-2/3; recorded here as the phase-wide
  format decision). **Backend-first**: no UI in P2-1 (P2-4).

## 1. Data model (one migration, import-safe: no `app.*` imports)

New table `production_sets`:

- `id` Integer PK autoincrement
- `production_id` Integer FK productions.id ondelete CASCADE, NOT NULL, indexed
- `name` String(255) NOT NULL — unique per production (`uq_prodset_name`)
- `status` String(20) NOT NULL, server_default `'draft'` — `draft` | `locked`
- `prefix` String(50) NOT NULL — e.g. `SMITH`
- `padding` Integer NOT NULL, server_default `6`
- `start_number` Integer NOT NULL, server_default `1`
- `sort_key` String(30) NOT NULL, server_default `'control_number'` —
  `control_number` | `custodian_date`
- `designation` String(100) nullable — set-wide confidentiality label
  (stamped by P2-2; e.g. "CONFIDENTIAL")
- `created_by` String(128) NOT NULL
- `created_at` DateTime server_default now() NOT NULL
- `locked_by` String(128) nullable
- `locked_at` DateTime nullable

New table `production_set_items`:

- `id` Integer PK autoincrement
- `production_set_id` Integer FK production_sets.id ondelete CASCADE, NOT NULL, indexed
- `document_id` UUID FK documents.id ondelete CASCADE, NOT NULL, indexed
- Unique `(production_set_id, document_id)` (`uq_prodset_item_doc`)
- Filled at lock, NULL while draft, immutable after:
  - `sort_order` Integer nullable
  - `bates_begin` String(50) nullable
  - `bates_end` String(50) nullable
  - `pages` Integer nullable — snapshot (1 for withheld, else doc.page_count)
  - `disposition` String(20) nullable — snapshot: `produce` | `redact_in_part` | `withhold`
- `designation` String(100) nullable — per-item override of the set default

No stored aggregate counts — list endpoints compute them.

## 2. Pure service — `app/services/production_numbering.py` (no DB/network)

```python
SORT_KEYS = frozenset({"control_number", "custodian_date"})

def format_bates(prefix: str, number: int, padding: int) -> str
# f"{prefix}{number:0{padding}d}" — numbers wider than padding grow naturally,
# never truncate (SMITH1000000 after SMITH999999 with padding 6).

def pages_for(disposition: str, page_count: int) -> int
# "withhold" -> 1 (the future slip-sheet page); else max(page_count, 1).

def order_members(members: list[MemberInfo], sort_key: str) -> list[MemberInfo]
# MemberInfo = (document_id, control_number, family_id, custodian, doc_date).
# Families stay contiguous: docs sharing a non-null family_id form a group;
# within a group, control-number order (parents ingest before attachments, so
# the parent sorts first). Standalone docs are their own group. Groups are
# interleaved by the group head's key: control_number -> head control number;
# custodian_date -> (custodian or "", doc_date or datetime.max, control number)
# — the trailing control number makes ordering total and deterministic.

def assign_bates(ordered: list[tuple[doc_id, pages]], prefix: str,
                 padding: int, start_number: int) -> list[Assignment]
# Assignment = (document_id, sort_order, bates_begin, bates_end).
# Gap-free: doc N's bates_begin is doc N-1's bates_end + 1; a doc's
# bates_end = bates_begin + pages - 1. sort_order counts from 1.
```

Disposition per doc comes from the existing `effective_disposition` in
`app/services/privilege.py` (privilege tags + redaction presence + per-doc
override) — no duplicated logic. Docs whose disposition is `None` (no
privilege, no redactions, no override) snapshot as `produce`.

## 3. Endpoints — new `app/routers/production_sets.py` (`/api` prefix)

Writes require manager+ on the matter; reads any role with matter access.
Every write is audit-logged via `log_action`.

- `POST /productions/{production_id}/production-sets` → 201 `ProductionSetOut`
  Body: `name`, `prefix`, optional `padding`, `start_number`, `sort_key`,
  `designation`. Creates a draft. 422 on empty/invalid `prefix` (must be
  non-empty, no whitespace) or unknown `sort_key`; 409 on duplicate name.
- `GET /productions/{production_id}/production-sets` → list of
  `ProductionSetOut` (+ computed `doc_count`).
- `GET /production-sets/{set_id}` → `ProductionSetOut` + counts
  (docs, and pages/Bates range once locked).
- `GET /production-sets/{set_id}/documents` → members ordered by
  `sort_order` (locked) or control number (draft), each with control number,
  assigned Bates (null in draft), pages, disposition, designation.
- `POST /production-sets/{set_id}/documents` → add members. Draft-only.
  Body: `document_ids: list[UUID] | None`, `tag_id: int | None`,
  `include_families: bool = false`, `exclude_duplicates: bool = false`.
  Union of explicit ids + docs bearing the tag; `include_families` pulls in
  all docs sharing a member's `family_id`; `exclude_duplicates` drops, per
  hash `DuplicateGroup`, every member except the primary (lowest control
  number) unless explicitly listed in `document_ids`. Docs outside the set's
  matter are rejected. Already-present docs are skipped, not errors.
  Returns `{added, skipped_existing, skipped_duplicates, families_added}`.
- `DELETE /production-sets/{set_id}/documents` → remove by ids. Draft-only.
- `DELETE /production-sets/{set_id}` → delete the set. Draft-only.
- `POST /production-sets/{set_id}/lock` → the one-way gate. Draft-only
  (409 if locked), 422 if the set is empty. In one transaction: compute each
  member's disposition, snapshot pages, order via `order_members`, assign via
  `assign_bates`, write all item rows, set `status='locked'`,
  `locked_by/locked_at`. Returns summary
  `{doc_count, page_count, bates_begin, bates_end}`.

Mutations against a locked set (add/remove/delete/lock) → 409. Unknown set →
404. `main.py` registers the router.

## 4. Error handling

- 403 before 404 checks follow the codebase convention (access first).
- Lock is all-or-nothing: any failure mid-assignment rolls back; a set is
  never partially numbered.
- `exclude_duplicates` and `include_families` interact: family expansion runs
  first, then duplicate filtering; an explicitly listed `document_id` is never
  dropped by the duplicate filter.

## 5. Testing (fake-session pattern, no DB; shared fakes in `tests/fakes.py`)

- Pure (`test_production_numbering.py`): `format_bates` padding/overflow;
  `pages_for` withhold=1; `order_members` family contiguity, parent-first,
  both sort keys, determinism with missing custodian/date; `assign_bates`
  gap-free continuity across mixed page counts and dispositions, sort_order
  sequence, start_number offset.
- Endpoints (`test_production_set_endpoints.py`): role gates (manager+ writes,
  reviewer blocked), draft/locked state machine (409s), add-by-tag,
  family expansion, duplicate skip + explicit-id exception, cross-matter doc
  rejection, lock summary + snapshot values, empty-set lock 422,
  duplicate-name 409.
- Migration: import-purity (no `app.*`), single head, py_compile.
- Full backend suite stays green (known pre-existing `test_ai_review` failure
  excepted).

## Out of scope (later sub-projects)

- Endorsement/stamping, slip-sheet pages, redaction burn-in of produced PDFs — P2-2.
- DAT/OPT writers, production manifest, Bates-continuity validation report,
  ZIP packaging, produced-text/searchable output — P2-3.
- Builder UI — P2-4. TIFF output — deferred behind PDF.
