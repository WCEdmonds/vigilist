# Multi-Load Matters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Note:** executed inline by the planning session; patches are applied against the exact anchors below.

**Goal:** Ingest additional loads (received productions or our own files) into an existing matter, namespaced per load, with control numbers continuing across loads.

**Architecture:** Optional `load_prefix` (`"loads/{load_id}/"`) threaded through the DAT/OPT bootstrap and PDF/native listing helpers, carried in `IngestJob.field_mapping` beside custodian/source fields; `control_offset` computed at process-start continues `{PREFIX} NNNNNN` numbering. Wizard gains an existing-matter target; every new ingest (new matter included) uses a load namespace. No migration.

**Tech Stack:** FastAPI, Firebase Storage listing, React, pytest fake-session tests.

**Spec:** `docs/superpowers/specs/2026-07-22-multi-load-matters-design.md`

## Global Constraints

- Branch `feat/multi-load-matters` (worktree `descubre-p0sp5`), off main.
- `load_id` validation: `^[A-Za-z0-9-]{1,32}$` → 422 otherwise; absent = legacy behavior (prefix `raw/` root, offset 0).
- `load_prefix`/`control_offset` ride `field_mapping` (never new columns); `load_prefix` always ends with `/`.
- Offset applies ONLY to native/generic_pdf (relativity Bates come from the DAT); offset is added to `global_index` at the batch call sites so `process_*_record` signatures stay untouched.
- Tests 0 warnings; frontend gate `npm run build`; no attribution trailers.

---

### Task 1: Backend — load-prefix threading + control offset

**Files:** `backend/app/services/ingest.py`, `ingest_pdf.py`, `ingest_native.py`; test `backend/tests/test_multi_load.py`.

- [ ] `compute_control_offset(bates_values, prefix) -> int` in `ingest.py` (regex `^{prefix} (\d+)$`, max tail, 0 default) + pure tests (empty list, mixed prefixes, gaps, non-numeric tails).
- [ ] `_download_dat_to_temp(production_id, load_prefix=None)` and `bootstrap_ingest_source(production_id, load_prefix=None)`: base prefix becomes `f"productions/{production_id}/raw/{load_prefix or ''}"`. `analyze_load_file(production_id, load_prefix=None)` passes through.
- [ ] `list_pdf_sources(production_id, load_prefix=None)` / `list_native_sources(production_id, load_prefix=None)`: same base-prefix change. Tests monkeypatch `list_files` and assert the requested prefix for both None and `"loads/x1/"`.
- [ ] Batch processors read `load_prefix` + `control_offset` from `job.field_mapping` and use them: `ingest_batch` → `bootstrap_ingest_source(production_id, load_prefix)`; `ingest_pdf_batch`/`ingest_native_batch` → namespaced listing and `offset + global_index` passed as the index argument to `process_pdf_record` / `process_native_email` / `process_native_record` (and in error-message control numbers). `ingest_from_storage` fallback counts use the same prefix.
- [ ] Commit: `feat(multi-load): load-prefix threading + control-number offset`.

### Task 2: Backend — endpoints

**Files:** `backend/app/routers/ingest.py`; tests appended to `test_multi_load.py`.

- [ ] `/ingest/process`: validate `load_id`; `load_prefix = f"loads/{load_id}/"` when present; for native/generic_pdf compute `control_offset` (query `Document.bates_begin` LIKE `f"{derive_bates_prefix(production.name)} %"` scoped to the matter → `compute_control_offset`); fold both into `field_mapping`; Cloud-Tasks total counting passes `load_prefix` to the listing/bootstrap helpers.
- [ ] `/ingest/analyze`: accept `load_id` (same validation), pass prefix to `analyze_load_file`.
- [ ] Tests: process folds prefix+offset (responder serves existing bates rows); 422 bad load_id; analyze passes prefix (monkeypatched `analyze_load_file` capturing args).
- [ ] Commit: `feat(multi-load): load-aware ingest endpoints`.

### Task 3: Frontend — wizard target + namespaced upload

**Files:** `frontend/src/api/client.ts`, `components/IngestWizard.tsx`, `App.tsx`.

- [ ] `client.ts`: `analyzeLoadFile(productionId, loadId?)` and `startProcessing(..., loadId?)` post `load_id`.
- [ ] `IngestWizard`: optional prop `existingProduction?: {id, name}`; target choice ("Add to {name}" default / "Start a new matter") shown when the prop exists, hiding the new-matter fields on add-to; `loadId` state (`crypto.randomUUID().slice(0, 8)`) used in the storage path `productions/{id}/raw/loads/{loadId}/{relativePath}` for ALL new ingests; add-to skips `createProductionForIngest` (and the token refresh, which exists to pick up new-matter claims); `loadId` passed to analyze + both process calls.
- [ ] `App.tsx`: in-matter mount passes `existingProduction={{ id: production.id, name: production.name }}`.
- [ ] `npm run build` green. Commit: `feat(multi-load): add-to-matter ingest wizard with per-load namespace`.

### Task 4: Verification + PR

- [ ] Full backend suite (known `test_ai_review` failure only); frontend build green.
- [ ] Push; PR to main `feat: multi-load matters — add loads to existing matters`.
