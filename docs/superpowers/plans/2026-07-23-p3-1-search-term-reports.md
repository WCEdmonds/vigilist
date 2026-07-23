# P3-1 Search-Term Hit Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Note:** executed inline by the planning session against the exact contracts in the spec; code authored at execution.

**Goal:** Durable, re-runnable per-term hit reports (hits / family-expanded / unique) with CSV export.

**Architecture:** One table (`search_term_reports`) persisting the term list + last run; a service that runs one FTS query per term plus one family-map query and one corpus count, computing expansion/uniqueness in Python; a small router registered in main.

**Spec:** `docs/superpowers/specs/2026-07-23-p3-1-search-term-reports-design.md`

## Global Constraints

- Branch `feat/p3-1-search-term-reports` (worktree). Migration `a3b4c5d6e7f8`, `down_revision = "f2a3b4c5d6e7"`, no `app.*` imports.
- Result JSON keys exactly as spec §2; CSV header exactly `Term,Documents with hits,Docs + families,Unique hits`.
- `source_type`: `received` exact; `collection` = `IS DISTINCT FROM 'received'`.
- Terms run through `build_tsquery`; a term that sanitizes to empty scores zeros (never errors).
- Writes manager+ & audited; reads any role with access. Tests fake-session, 0 warnings; no attribution trailers.

### Task 1: Migration + model
- [ ] `backend/alembic/versions/a3b4c5d6e7f8_add_search_term_reports.py` per spec §1; `SearchTermReport` model after `ProductionSetItem` in models.py; py_compile + import + purity + single-head grep; commit.

### Task 2: Service + tests
- [ ] `backend/app/services/search_terms.py` — `run_search_term_report` per spec §2 (per-term `select(Document.id, Document.family_id)` with `text_search_vector @@ to_tsquery`; family map query filtered `family_id IS NOT NULL`; count query; Python expansion/uniqueness; `computed_at` iso-utc).
- [ ] `backend/tests/test_search_terms.py` — fake-session; per-term queries served by a queue-popping callable responder on substring `"@@"`; family map on `"family_id IS NOT NULL"`; count on `"count"`. Cases: two overlapping terms (uniqueness), family expansion pulls a non-hit sibling, empty-sanitized term zeros, source_type threads into SQL (`IS DISTINCT FROM` for collection). Commit.

### Task 3: Schemas + router + tests
- [ ] Schemas `SearchTermReportCreate {name, terms}`, `SearchTermReportOut` (all columns, from_attributes). Router `backend/app/routers/search_terms.py` with the six endpoints per spec §3 (validation: name non-blank; 1..200 terms; every term non-blank). Register in `main.py`.
- [ ] Endpoint tests appended to `test_search_terms.py`: create (+422s ×3, 403 reviewer), run persists results + computed_at (monkeypatched service) + audit called, detail/list, csv shape + TOTAL row + 404-before-run, delete manager-only. Commit.

### Task 4: Verify + PR
- [ ] Full suite (known `test_ai_review` failure only); migration re-check; push; PR to main.
