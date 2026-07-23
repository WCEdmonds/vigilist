# P2-4 — Production Builder UI

**Date:** 2026-07-22
**Phase:** 2 (final sub-project), on main (P2-1…P2-3.5 + P0-SP5 all merged)
**Depends on:** every production-set endpoint built in P2-1…P2-3.5; P0-SP5 workspace mode
**Consumed by:** end users producing documents.

## Decision context (design approved in conversation, 2026-07-22)

- **Console-style, status-driven flow** modeled on Relativity's Stage →
  Validate → Produce → Brand → Export: the builder shows the step the set is
  actually in (derived from `status` / `render_status` / `package_status`),
  not a free-form wizard the user can wander out of sync.
- **The Validate panel is the centerpiece**: five conflict categories
  (qc_pending, privilege_produce, no_images, received_document — plus
  whatever future categories the endpoint returns are displayed generically)
  with control-number links into the viewer; Lock is disabled while
  conflicts exist unless an explicit "Override conflicts (recorded)"
  checkbox is ticked — mirroring the audited backend override.
- **Lives in Outgoing mode**: the `ProductionSetsPanel` renders in the
  content area when the P0-SP5 workspace toggle is on Outgoing — the panel
  re-emphasis promised in that spec.
- `exclude_received` defaults ON in the add-documents controls;
  `exclude_duplicates` defaults ON; `include_families` defaults ON
  (families-travel-together is the e-discovery norm).
- "Continue from previous": the create form suggests
  `start_number` = (max end number across the matter's locked sets) + 1 when
  prior sets share the prefix; shown as a hint, user-editable.
- Downloads (spot-check PDFs, package ZIP) use the authed-fetch→blob
  pattern (`fetchDocumentPdf` precedent); the browser follows the 307 to
  the signed URL with the auth header stripped cross-origin.

## 1. API layer — `frontend/src/api/client.ts`

Exported types: `ProductionSetInfo` (full `ProductionSetOut` mirror),
`ProductionSetMember`, `ValidationConflict`, `ValidationReport`,
`ManifestReport`. Functions: `listProductionSets`, `createProductionSet`,
`getProductionSet`, `getProductionSetMembers`,
`addProductionSetDocuments`, `removeProductionSetDocuments`,
`deleteProductionSet`, `getProductionSetValidation`, `lockProductionSet`
(with `overrideConflicts`), `renderProductionSet`,
`getProductionSetManifest`, `packageProductionSet`, `fetchProducedPdf`,
`fetchProductionPackage` (blob helpers).

## 2. `ProductionSetsPanel.tsx`

Card listing the matter's production sets: name, status chip
(draft / locked / rendering n/N / rendered / error / packaged), doc count,
Bates range; "New production set" button. Opens `ProductionBuilder` as a
modal for a chosen set (or `'new'`). Polls the list every 8s while any set
is rendering/packaging. Mounted in `App.tsx` when `workMode === 'outgoing'`.

## 3. `ProductionBuilder.tsx` (modal, `modal-overlay`/`modal-panel` pattern)

Status-driven sections:

- **Create** (`setId === 'new'`): name, prefix, padding, start number (with
  continue-from-previous hint), sort key, designation → POST → draft.
- **Draft**: add-documents controls (tag dropdown, "Add N selected" using
  the app's `selectedIds`, three toggles with the defaults above), add
  result breakdown (added / skipped existing / duplicates / received /
  families), member count, remove-selected, delete-set; **validation
  panel** (auto-refreshed after every add, manual Re-check button) with the
  conflict lists and the lock control (disabled on conflicts unless the
  override checkbox is ticked; checkbox text: "Override conflicts — this is
  recorded with my name").
- **Locked, not rendered**: lock summary (docs, pages, Bates range,
  disposition counts from the manifest endpoint) + Render button.
- **Rendering**: progress `rendered_count / doc_count`, 3s polling;
  error state shows `render_error` + Re-render.
- **Rendered**: manifest view — counts, Bates range, continuity OK/error
  list (green/red) — Package button; member table (first 100) with
  per-doc PDF spot-check download links.
- **Packaging / Packaged**: poll; then Download package (ZIP blob) +
  `package_path` shown; Re-package allowed.

## 4. Testing / verification

- No frontend test runner exists; the gate is `npm run build` (tsc strict)
  plus backend suite unchanged.
- Backend: no changes in this sub-project.

## Out of scope

- Per-item designation editing, numbering-type variants, placeholder
  customization (future backend features).
- Panel placement changes for Incoming mode (unchanged).
