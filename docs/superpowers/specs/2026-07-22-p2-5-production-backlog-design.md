# P2-5 — Production Backlog: Bates Lookup, Native Production, TIFF, Volumes

**Date:** 2026-07-22
**Phase:** 2 backlog (deferred items from P2-1…P2-4 specs), off main
**Depends on:** merged Phase 2 stack; independent of open PR #43 (frontend-only)

## Decision context

Four deliberately-deferred items, one branch, sequential commits:

1. **Produced-Bates lookup** — searches and AI citations must resolve
   *produced* numbers ("SMITH000123"), not just ingest control numbers.
2. **Native-file production** — spreadsheets etc. produced as native files
   (named by Bates) with a "PRODUCED IN NATIVE FORMAT" slip-sheet image and
   a NATIVELINK load-file column — standard protocol for documents that are
   meaningless as page images.
3. **Per-page Group 4 TIFF + page-level OPT** — the classic
   Relativity-compatible image format, per-set `image_format` choice.
4. **Volume chunking** — size-capped `VOL001/`, `VOL002/`… directories in
   the package with load-file paths reflecting volumes.

## 1. Data model (ONE migration `f2a3b4c5d6e7`, down_revision `e1f2a3b4c5d6`)

- `production_sets.image_format` — String(10) NOT NULL default `'pdf'`
  (`pdf` | `tiff`).
- `production_sets.native_file_types` — JSONB NOT NULL default `[]` — list
  of `Document.file_type` values produced natively (e.g. `["spreadsheet"]`).
- `production_sets.volume_max_mb` — Integer nullable (NULL = one volume).
- `production_set_items.produce_native` — Boolean NOT NULL default false —
  snapshot at lock.

## 2. Produced-Bates lookup

`app/services/produced_bates.py`: `resolve_produced_bates(db, accessible_ids,
production_id, bates) -> document_id | None`. Normalizes the query
(alnum-only, upper), splits alpha prefix + number, finds LOCKED sets in
scope whose normalized prefix matches, reformats with the set's padding via
`format_bates`, and range-matches `bates_begin <= q <= bates_end` (string
compare is ordered within a set's fixed padding). Wired into
`GET /api/documents/by-bates` as the third fallback (after exact and
normalized control-number matches); the resolved doc is re-fetched through
the same access scoping.

## 3. Native-file production

- Create endpoint accepts `image_format`, `native_file_types`,
  `volume_max_mb` (422 on bad values: unknown format, volume_max_mb < 50).
- **Lock** snapshots `item.produce_native = (disposition == "produce" AND
  doc.file_type ∈ ps.native_file_types AND doc.native_path IS NOT NULL)`;
  `pages_for` unchanged, but native items page-count = 1 (their image
  counterpart is a single slip-sheet).
- **Render**: `produce_native` items render as
  `slip_sheet(..., title="PRODUCED IN NATIVE FORMAT")`.
- **Package**: native bytes (GCS or local, `_load_page`-style selection)
  land at `{vol}/NATIVES/{bates_begin}{ext}`; DAT gains a `NATIVELINK`
  column (appended to `DAT_COLUMNS`) pointing there; blank for non-native
  rows. Redacted/withheld docs are NEVER produced natively (rule enforced
  at lock: only `produce` disposition qualifies).

## 4. TIFF rendering + page-level OPT

- `image_format == "tiff"`: each final page (slip-sheets included) is
  converted to 1-bit and uploaded as Group 4 TIFF at
  `productions/{pid}/production_sets/{sid}/tiff/{page_bates}.tif`;
  `item.output_path` = the first page's path (progress/idempotency marker
  unchanged). No per-doc PDF is built; the spot-check PDF endpoint returns
  409 for TIFF sets (package is the artifact).
- **Package (tiff)**: pages land at `{vol}/IMAGES/{page_bates}.tif`; OPT is
  page-level — first page row `bates,vol,path,Y,,,{pages}`, continuation
  rows `pagebates,vol,path,,,,` — exactly the shape `parse_opt` groups.
  Manifest artifacts carry per-doc combined sha256 (pages hashed in order)
  + total bytes + page count.

## 5. Volume chunking

- Packaging assigns documents (in sort order, whole documents only) to
  volumes greedily by artifact bytes (images + native + text) against
  `volume_max_mb`; NULL → everything in `VOL001`.
- Zip layout becomes volume-first ALWAYS (breaking change to the P2-3
  layout, tests updated): `VOL001/PDFS|IMAGES/…`, `VOL001/NATIVES/…`,
  `VOL001/TEXT/…`, with `DATA/{prefix}.dat|.opt` and `manifest.json` at the
  root. DAT `TEXTPATH`/`NATIVELINK` and OPT paths include the volume dir
  (`.\VOL001\…`). OPT volume column = the doc's actual volume label.
- Manifest gains `volumes: [{label, documents, bytes}]`.

## 6. Testing

- Pure: prefix/number parsing + padding reformat for lookup; TIFF page
  path/naming; page-level OPT round-trip through `parse_opt`; volume
  assignment (greedy, whole docs, NULL cap).
- Fake-session: by-bates fallback resolution; lock snapshots
  `produce_native`; render TIFF branch uploads per page (monkeypatched
  storage) and PDF endpoint 409; package native/TIFF/volume layouts
  verified by reading the produced zip (existing capture pattern);
  create-endpoint 422s.
- Full backend suite green; migration purity/single-head.

## Out of scope

- Frontend controls for the new set options (small P2-4 follow-up once
  merged; API accepts them now).
- Native redaction (burning into natives) — never planned; redacted docs
  stay image-produced.
