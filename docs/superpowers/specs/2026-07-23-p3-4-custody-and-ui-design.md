# P3-4 — Chain of Custody, Exceptions + Phase-3 UI (combined)

**Date:** 2026-07-23
**Phase:** 3, final sub-project (P3-4 + UI merged per user direction)
**Depends on:** everything — this assembles records earlier phases keep.
Stacked on P3-3 (PR #60).

## Decision context (approved 2026-07-23)

Pure assembly: **no migrations, no new tables** — the audit log, ingest
jobs, hashes, review results, samples, validation reports, and production
sets already hold the record; P3-4 reads it back as three read-only report
endpoints, and the UI puts all of Phase 3 on screen.

## 1. Backend — new `app/routers/defensibility.py` (`/api`), all read-only, any role with access

- `GET /documents/{id}/lineage` — per-document story, sectioned (not a
  forced timeline; ingest predates per-doc timestamps):
  `identity` (control number, source party/type, source_path, file
  name/type, md5/sha256, extraction status/error), `tags`
  (name, applied_by/at), `review` (per project: ai_decision, confidence,
  attorney_decision, created_at), `redactions` (count + QC decisions
  decided_by/at/decision), `productions` (per set membership: set name,
  status, bates range, disposition, produce_native, output_path),
  `audit` (entries with resource_id == doc id: action, user_email,
  created_at, details) — capped at the most recent 200.
- `GET /productions/{id}/exceptions` — the honest ledger:
  `{counts: {status: n}, total, exceptions: [{document_id, control_number,
  file_name, source_party, extraction_status, extraction_error}]}` for
  `extraction_status != 'ok'`; `.../exceptions/csv` for exchange.
- `GET /productions/{id}/chain-of-custody` — matter-level processing
  report: `loads` (ingest jobs: format, status, counts, source
  party/type from field_mapping, created/completed), `documents`
  (total, by_source_type, by_extraction_status, hashed count),
  `review` (projects + processed counts + latest validation summary:
  recall/elusion rates), `productions` (sets: name, status, bates range,
  render/package status, packaged_at, conflicts_overridden_by).

## 2. Frontend — `DefensibilityPanel.tsx` card

Mounted in the content area for ALL workspace modes (defensibility spans
both directions), collapsible, with four sections:

- **Search terms**: list reports; create (name + one-term-per-line
  textarea); Run; last-run table (term / hits / +families / unique) and CSV
  download; source-scope select.
- **Sampling**: list samples (name, purpose, size, drawn date); draw form
  (name, purpose, size-or-auto, source scope; elusion purpose exposes a
  review-project select); estimate row — pick a tag, see rate + CI +
  extrapolation inline.
- **TAR validation**: run form (project, control sample, responsive /
  non-responsive tags, optional elusion sample, confidence); report list;
  latest report rendered as the headline numbers — recall / precision /
  elusion with CIs, confusion matrix, notes.
- **Custody & exceptions**: chain-of-custody summary (loads, doc counts,
  production sets) + exceptions count with CSV download.

API layer: typed wrappers for the seven Phase-3 endpoint groups
(search-term reports, samples/estimates, tar-validation, lineage,
exceptions, chain-of-custody) using the established request/blob patterns.
Document viewer gets no lineage tab yet (endpoint is UI-ready; viewer
integration can ride a later polish pass).

## 3. Testing

Backend fake-session tests per endpoint: lineage assembles all sections
(responders per table substring; queued where shapes collide), exceptions
filters + counts + CSV shape, chain-of-custody aggregates. Frontend gate:
`npm run build`. Full suite green.

## Out of scope

- Hash RE-verification jobs (downloading natives to recompute — future).
- Document-viewer lineage tab; PDF-formatted custody report.
