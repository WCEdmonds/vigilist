# P2-4 Production Builder UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Note:** executed inline by the planning session (full context held); the two new components are authored at execution against the exact contracts below rather than duplicated verbatim here.

**Goal:** Console-style production builder UI: set list panel in Outgoing mode, status-driven builder modal (create → build/validate → lock → render → package → download) over the P2-1…P2-3.5 endpoints.

**Architecture:** All new code is frontend: an API layer block in `client.ts` (typed wrappers + authed blob helpers), `ProductionSetsPanel.tsx` (list + polling + modal host), `ProductionBuilder.tsx` (status-driven modal using the app's `modal-overlay`/`modal-panel` pattern), one `App.tsx` mount point gated on `workMode === 'outgoing'`. Zero backend changes.

**Tech Stack:** React 19 + TypeScript, existing fetch/auth patterns.

**Spec:** `docs/superpowers/specs/2026-07-22-p2-4-production-builder-ui-design.md`

## Global Constraints

- Branch `feat/p2-4-production-builder-ui` off main (worktree `descubre-p0sp5`).
- Verification gate: `npm run build` (tsc strict + vite) — no frontend test runner exists; backend suite untouched.
- Downloads use the authed-fetch→blob pattern (`fetchDocumentPdf` precedent); never `window.open` (auth header required on the API leg).
- Defaults in add-documents controls: `include_families` ON, `exclude_duplicates` ON, `exclude_received` ON.
- Lock button disabled while `validation.total > 0` unless the "Override conflicts — this is recorded with my name" checkbox is ticked; the checkbox maps to `override_conflicts` in the lock body.
- No AI-attribution trailers.

---

### Task 1: API layer (`frontend/src/api/client.ts`)

- [ ] Append a `// ── Production sets (P2) ──` block exporting:
  - Types: `ProductionSetInfo` (all `ProductionSetOut` fields, timestamps as `string | null`), `ProductionSetMember`, `ValidationConflict`, `ValidationReport` (`qc_pending | privilege_produce | no_images | received_document: ValidationConflict[]; total: number`), `ManifestReport` (`production_set: Record<string, unknown>`, `counts: Record<string, number>`, `bates_range`, `continuity: {ok, errors}`, `artifacts[]`, `generated_at`).
  - Functions (exact endpoint paths): `listProductionSets(productionId)` GET `/api/productions/{id}/production-sets`; `createProductionSet(productionId, body)` POST same; `getProductionSet(setId)`; `getProductionSetMembers(setId)` GET `.../documents`; `addProductionSetDocuments(setId, body)` POST `.../documents` → `{added, skipped_existing, skipped_duplicates, families_added, skipped_received}`; `removeProductionSetDocuments(setId, ids)` DELETE `.../documents` with JSON body; `deleteProductionSet(setId)` DELETE; `getProductionSetValidation(setId)` GET `.../validation`; `lockProductionSet(setId, overrideConflicts=false)` POST `.../lock` with `{override_conflicts}`; `renderProductionSet(setId)` POST `.../render`; `getProductionSetManifest(setId)` GET `.../manifest`; `packageProductionSet(setId)` POST `.../package`; `fetchProducedPdf(setId, documentId)` and `fetchProductionPackage(setId)` via a shared `authedBlob(url)` helper (clone of `fetchDocumentPdf`'s auth/error handling).
- [ ] `npm run build` passes. Commit: `feat(p2-4): production-set API layer`.

### Task 2: `ProductionSetsPanel.tsx`

- [ ] Create `frontend/src/components/ProductionSetsPanel.tsx`:
  - Props `{ productionId: number; tags: Tag[]; selectedIds: Set<string>; onOpenDoc: (id: string) => void }`.
  - Loads sets on mount; card with "Production Sets" title + count, "New production set" button; one row per set (name; doc count; Bates range when locked; status chip: Draft / Locked / Rendering n/N / Rendered / Packaging… / Packaged / Error).
  - Polls `listProductionSets` every 8s while any set is `rendering`/`packaging`.
  - Hosts `ProductionBuilder` modal (`setId: number | 'new'`), passing the loaded `sets` for the continue-from-previous hint; refreshes list on close.
- [ ] Commit with Task 3 (panel imports the builder).

### Task 3: `ProductionBuilder.tsx`

- [ ] Create `frontend/src/components/ProductionBuilder.tsx` (modal, `modal-overlay`/`modal-panel`, width 640, scrollable):
  - Props `{ productionId; setId: number | 'new'; tags; selectedIds; existingSets: ProductionSetInfo[]; onOpenDoc; onClose }`.
  - **Create form** (`setId === 'new'`, no set yet): name, prefix (default `PROD`), padding (default 6), start number with continue-from-previous auto-fill (max numeric `bates_end` tail across locked sets sharing the prefix, + 1, with a hint line), sort key select (`control_number` / `custodian_date`), designation input. Create → holds the returned draft set.
  - **Draft view**: member count + last-add breakdown line; add controls (tag dropdown + "Add by tag", "Add N selected" from `selectedIds`, three default-ON toggles); validation panel auto-refreshed after every add with Re-check button — green "No conflicts" or per-category lists (`CONFLICT_LABELS` map for the four known categories) where each entry is a link (`onOpenDoc(document_id)`) showing control number + detail; override checkbox (only when conflicts exist); Lock button (disabled per Global Constraints); Delete draft.
  - **Locked view**: summary (docs / pages / Bates range / designation); manifest counts + continuity (green OK / red error list); then by status: `render_status` not_started → Render; rendering → `rendered_count/doc_count` progress with 3s poll; error → `render_error` + Re-render; rendered → package controls (`package_status`: not_started → Package; packaging → poll; error → message + retry; packaged → Download ZIP via blob). Member table (first 100: Bates, control number, disposition, pages) with per-doc spot-check PDF download once rendered.
  - All mutations run through a shared `run()` wrapper (busy flag + error banner).
- [ ] `npm run build` passes. Commit: `feat(p2-4): production sets panel + status-driven builder modal`.

### Task 4: App integration + verification + PR

- [ ] `App.tsx`: import panel; render after the workspace-mode toggle block, gated `workMode === 'outgoing'`, wired to `production.id`, `allTags`, `selectedIds`, `setViewDocId`.
- [ ] `npm run build` green; full backend suite still green (no backend diff — sanity only).
- [ ] Push; PR to main titled `feat(p2-4): production builder UI`, body covering the console flow, validation-panel centerpiece, Outgoing-mode placement. Commit: `feat(p2-4): mount production panel in outgoing mode`.
