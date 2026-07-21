# Phase 1 · P1-1 — Redaction Data Model

**Date:** 2026-07-21
**Status:** Approved design, pending implementation plan
**Roadmap:** `docs/ediscovery-parity-roadmap.md` (Phase 1 — Redaction & Privilege). Part of the **produce-out milestone** (Phase 1 + Phase 2): the goal is producing documents *out* to opposing counsel/regulators, and redaction is the required first step.
**Branch:** `feat/phase1-p1-1-redaction-model`
**Builds on:** Phase 0 (document/image model, `Annotation` coordinate convention, role model). Backend-first per the sequencing decision (UI comes later on the redesign branch).

## Summary

Store rectangular **redaction regions** on document pages, each with a reason code, plus CRUD
endpoints and access control. This is the data foundation for the produce-out redaction work.
It is **data-only**: storing coordinates exposes nothing and applies nothing — image burn-in,
per-region text masking, QC gating, and production output are later sub-projects (P1-2…P2).
Because it only adds a table, it ships to prod safely with no behavior change.

Redaction is a **production-time** concept: reviewers mark regions during review; the regions
are later burned into produced images and used to remove covered words from produced text/
load-files (P1-2/Phase 2). The internal review API keeps serving full text (reviewers need it to
make redaction/privilege calls) — airtightness lives at the export boundary, not here.

## Current state (verified)

- `Annotation` (`backend/app/models.py:367-383`) is the coordinate-model to mirror: `id`
  Integer autoincrement PK; `document_id` `UUID(as_uuid=True)` FK `ondelete="CASCADE"`;
  `page_num` Integer; `x_pct`/`y_pct` Float (0–100 normalized); `created_by` String(128);
  `created_at` DateTime `server_default=func.now()`; indexes on `document_id` and `created_by`.
- Annotation CRUD (`backend/app/routers/annotations.py`) is the endpoint pattern: list/create/
  update/delete, access-scoped, validates `page_num ≤ doc.page_count` and `0 ≤ x/y_pct ≤ 100`,
  create requires **not readonly**, edit/delete by **creator or manager+**.
- Roles (`backend/app/dependencies.py`): `ROLE_RANK = {admin:4, manager:3, reviewer:2,
  readonly:1}`; `get_accessible_production_ids`, `get_user_role_for_production`.
- Audit: `backend/app/services/audit.py` `log_action(db, user, action, entity_type, entity_id,
  production_id=..., details=...)` — used by higher-stakes actions.
- Alembic single head: `adfc16bff9f3` (the P1-1 migration's `down_revision`).
- `Document.id` is a UUID; `Document.page_count` bounds `page_num`.

## Design

### 1. `Redaction` model (`backend/app/models.py`)

New table `redactions`, mirroring `Annotation`'s conventions:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK, autoincrement | |
| `document_id` | `UUID(as_uuid=True)` FK → `documents.id` `ondelete=CASCADE`, not null | indexed |
| `page_num` | Integer, not null | 1-indexed, `≤ document.page_count` |
| `x_pct` | Float, not null | top-left X, 0–100 normalized |
| `y_pct` | Float, not null | top-left Y, 0–100 |
| `w_pct` | Float, not null | width, `> 0`, `x_pct + w_pct ≤ 100` |
| `h_pct` | Float, not null | height, `> 0`, `y_pct + h_pct ≤ 100` |
| `reason_code` | String(40), not null | one of the defined set (below) |
| `note` | Text, nullable | optional free text (e.g. for `other`) |
| `created_by` | String(128), not null | user id |
| `created_at` | DateTime, `server_default=func.now()`, not null | |
| `updated_at` | DateTime, `onupdate=func.now()`, nullable | |

Indexes: `ix_redactions_document_id` on `document_id`; `ix_redactions_doc_page` on
`(document_id, page_num)`. Rectangle uses top-left origin + width/height, all normalized 0–100
(same convention as `Annotation.x_pct/y_pct`). QC status (P1-4), the redacted-word mapping
(P1-2), and disposition (P1-3) are intentionally **not** columns here.

### 2. Reason codes (`backend/app/services/redaction.py`, new)

A defined, validated set (extensible constant): `attorney_client`, `work_product`, `pii`,
`phi`, `confidential`, `trade_secret`, `non_responsive`, `other`. A `REDACTION_REASON_CODES`
frozenset + a helper `is_valid_reason_code(code) -> bool`. `note` is optional but recommended
for `other`.

### 3. Endpoints (`backend/app/routers/redactions.py`, new — mirror annotations)

- `GET /documents/{doc_id}/redactions` → `list[RedactionOut]`, ordered by `page_num`,
  `created_at`. Access-scoped to the user's accessible productions (404 if the doc isn't
  accessible).
- `POST /documents/{doc_id}/redactions` → `RedactionOut`. Requires **not readonly** (reviewer+).
  Validates: `1 ≤ page_num ≤ doc.page_count`; `0 ≤ x_pct,y_pct ≤ 100`; `w_pct,h_pct > 0`;
  `x_pct+w_pct ≤ 100`; `y_pct+h_pct ≤ 100`; `reason_code ∈ REDACTION_REASON_CODES` (else 422/400).
- `PUT /redactions/{redaction_id}` → update rectangle / `reason_code` / `note`. **Creator or
  manager+**. Same validation.
- `DELETE /redactions/{redaction_id}` → 204. **Creator or manager+**.
- Each write calls `log_action` (`redaction_created` / `redaction_updated` / `redaction_deleted`,
  with `document_id`/`page_num`/`reason_code` in details) — redaction actions are
  defensibility-relevant and must be audited. Router registered in `backend/app/main.py`.

### 4. Schemas (`backend/app/schemas.py`)

- `RedactionCreate { page_num, x_pct, y_pct, w_pct, h_pct, reason_code, note? }`
- `RedactionUpdate { x_pct?, y_pct?, w_pct?, h_pct?, reason_code?, note? }` (all optional)
- `RedactionOut { id, document_id, page_num, x_pct, y_pct, w_pct, h_pct, reason_code, note,
  created_by, created_at, updated_at }`

### 5. Migration

Additive: `create_table("redactions", ...)` + the two indexes. `down_revision = adfc16bff9f3`.
`downgrade()` drops the table. Verify upgrade/downgrade/re-upgrade against a throwaway
`pgvector/pgvector:pg16` Postgres before merge. No data backfill; nothing to prod-migrate beyond
the new table.

## Testing

Deterministic tests (`backend/tests/`, run via `venv/Scripts/python.exe -m pytest`):
- Reason-code validation: valid set accepted; unknown rejected (`is_valid_reason_code`).
- Rectangle validation (pure helper `validate_rect(page_num, x, y, w, h, page_count) -> error|None`):
  out-of-page `page_num`, negative/over-100 coords, `w/h ≤ 0`, `x+w > 100`, `y+h > 100` all
  rejected; a valid box accepted. This pure validator is unit-tested and reused by POST/PUT.
- Access control (endpoint-level, existing test harness): create blocked for readonly; edit/
  delete blocked for a non-creator reviewer, allowed for manager+; list/read scoped to accessible
  productions.
- CRUD roundtrip: create → list → update → delete; cascade delete when the parent Document is
  deleted (FK `ondelete=CASCADE`).

## Out of scope (P1-1)
- Image burn-in, on-demand word geometry, per-region text masking, redacted preview — **P1-2**.
- Privilege tagging / privilege log / withhold-vs-redact-in-part disposition — **P1-3**.
- Redaction QC status + gating — **P1-4**.
- Production-set assembly / Bates / load-file export — **Phase 2**.
- Any change to the internal text/search APIs (they intentionally keep serving full text).
