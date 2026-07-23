# P2-3 — Load Files, Manifest, Packaging

**Date:** 2026-07-22
**Phase:** 2 (Production / Deliverable Output), sub-project P2-3
**Depends on:** P2-2 (rendered artifacts, PR #39 — stacked branch), P2-1 (set model/numbering)
**Consumed by:** P2-4 (UI); the delivered ZIP goes to opposing counsel.

## Decision context (brainstormed 2026-07-22)

- **Our importers are the compliance oracle.** DAT/OPT writers mirror the
  exact conventions of `utils/parsers.py` / `utils/loadfile.py` (þ `þ`
  wrapper, DC4 `\x14` separator, UTF-8 BOM, CRLF), and the round-trip tests
  parse our output with `parse_dat` / `parse_opt` to prove compatibility.
- **PDF-production OPT**: one line per document referencing its produced PDF
  (`BATES,VOL{id:03d},.\PDFS\{bates}.pdf,Y,,,{pages}`). Classic per-page
  TIFF OPT arrives with TIFF support (deferred behind PDF).
- **Privilege safety in the deliverable**: withheld rows blank SUBJECT and
  FILENAME (privilege-log-equivalent metadata only); TEXT files ship ONLY
  for `produce` documents (stored `text_content` is pre-redaction, so
  redacted docs must never ship text). Withheld/redacted text is simply
  absent from the package.
- **Manifest on demand + in the package.** `GET .../manifest` computes
  counts/range/continuity instantly for any locked set (no packaging, no
  hashes); the packaged `manifest.json` additionally carries per-artifact
  SHA-256 computed while zipping.
- **One packaging task, no batching** — a ZIP build is sequential. Cloud
  Task (1800s deadline) or BackgroundTasks fallback, mirroring render.
- **Re-package allowed** after completion/error; deterministic path
  overwrites. 409 only while `packaging` or when prerequisites unmet.

## 1. Data model (one migration, import-safe)

`production_sets`: `package_status` String(20) NOT NULL server_default
`'not_started'` (`not_started` | `packaging` | `packaged` | `error`);
`package_error` Text nullable; `package_path` String(500) nullable;
`packaged_at` DateTime nullable. `down_revision = "b8c9d0e1f2a3"`.

## 2. Pure writers — `app/services/loadfile_export.py` (no DB/network)

```python
DAT_COLUMNS = ["BEGBATES", "ENDBATES", "BEGATTACH", "ENDATTACH", "CUSTODIAN",
               "FROM", "TO", "CC", "DATESENT", "DATERECEIVED", "SUBJECT",
               "FILENAME", "FILETYPE", "MD5HASH", "SHA256HASH", "PAGECOUNT",
               "REDACTED", "WITHHELD", "CONFIDENTIALITY", "TEXTPATH"]

def dat_bytes(rows: list[dict]) -> bytes
# UTF-8 BOM + header row + one row per doc; every field þ-wrapped, DC4
# separator, CRLF line ends. Missing keys -> "". Values coerced to str;
# embedded þ / DC4 / CR / LF are stripped from values (they would corrupt
# the format and never legitimately occur in metadata).

def opt_bytes(entries: list[tuple[str, str, str, int]]) -> bytes
# entries = (bates_begin, volume, pdf_path, page_count) ->
# "{bates},{volume},{path},Y,,,{pages}\r\n" per doc, UTF-8 (no BOM needed;
# parse_opt handles utf-8-sig either way).

def check_continuity(items: list[tuple[str, str, int]], prefix: str,
                     start_number: int) -> list[str]
# items = (bates_begin, bates_end, pages) in sort_order. Violations:
# - first doc's begin number != start_number
# - any doc's end != begin + pages - 1
# - any doc's begin != previous end + 1  (gap or overlap)
# Returns human-readable error strings; [] = continuous.

def manifest_dict(ps_info: dict, counts: dict, bates_range: dict,
                  continuity_errors: list[str],
                  artifacts: list[dict]) -> dict
# {"production_set": ps_info, "counts": counts, "bates_range": bates_range,
#  "continuity": {"ok": not errors, "errors": errors},
#  "artifacts": artifacts, "generated_at": iso-utc}
```

## 3. Export assembly — `app/services/production_export.py` (DB + storage)

```python
def package_path_for(ps) -> str
# f"productions/{ps.production_id}/production_sets/{ps.id}/package/{ps.prefix}_production.zip"

async def build_dat_rows(db, ps, items) -> list[dict]
# Queries Documents for the items; per row:
# - BEGBATES/ENDBATES from the item snapshot; PAGECOUNT = item.pages
# - BEGATTACH/ENDATTACH: family range within the set (docs sharing a
#   non-null family_id: min begin / max end across the family's items,
#   ordered by sort_order); standalone docs use their own begin/end
# - CUSTODIAN/FROM/TO/CC/DATESENT/DATERECEIVED (dates as YYYY-MM-DD)
# - SUBJECT = email_subject, FILENAME = file_name, FILETYPE, MD5HASH,
#   SHA256HASH from the Document
# - REDACTED = "Y" iff disposition == "redact_in_part", WITHHELD = "Y" iff
#   "withhold"; CONFIDENTIALITY = item.designation or ps.designation or ""
# - withhold rows: SUBJECT = "" and FILENAME = "" (privilege safety)
# - TEXTPATH = ".\\TEXT\\{bates_begin}.txt" ONLY for produce docs with
#   non-empty text_content; else ""

async def compute_manifest(db, ps, items, artifact_hashes=None) -> dict
# counts by disposition + pages, bates range from first/last item,
# check_continuity(...) over the snapshots, artifacts =
# [{bates_begin, path: item.output_path, **({"sha256", "bytes"} if hashes)}]

async def package_set(db, set_id: int) -> None
# Job body (assumes trigger already set package_status="packaging"):
# load ps + items (sort_order); guard status=="locked" and
# render_status=="rendered" (else mark error). Build DAT rows + OPT entries.
# Stream a zip to a NamedTemporaryFile:
#   DATA/{prefix}.dat, DATA/{prefix}.opt,
#   PDFS/{bates_begin}.pdf  (storage.get_download_bytes(item.output_path);
#                            sha256 + size recorded per artifact),
#   TEXT/{bates_begin}.txt  (produce docs with text_content, UTF-8),
#   manifest.json           (compute_manifest with hashes)
# storage.upload_file(tmp, package_path_for(ps), "application/zip");
# package_status="packaged", packaged_at=now, package_path set. Any
# exception -> package_status="error" + message, commit, return (no raise).
# Temp file removed in a finally block.
```

## 4. Task fan-out — `tasks.enqueue_package(set_id)`

Same shape as `enqueue_render_batch`; handler URL
`/api/production-sets/package-worker`; deadline 1800s.

## 5. Endpoints — extend `app/routers/production_sets.py`

- `GET /production-sets/{set_id}/manifest` — any role with access; 409 if
  not locked. Returns `compute_manifest` (no hashes) — instant validation.
- `POST /production-sets/{set_id}/package` — manager+; 409 unless
  `render_status == "rendered"`; 409 while `packaging`. Sets
  `package_status="packaging"`, clears error/path/timestamp, audit-logs,
  commits; enqueues Cloud Task or BackgroundTasks fallback
  (`_package_inline` on a fresh session). Returns `{"documents": N}`.
- `POST /production-sets/package-worker` — OIDC-guarded; body `{set_id}`;
  runs `package_set`; always 200 on render logic errors (they land in
  `package_status="error"`).
- `GET /production-sets/{set_id}/package` — 404 unless
  `package_status == "packaged"`; 307 redirect to
  `get_signed_url(package_path, response_disposition=attachment zip)`.
- `ProductionSetOut` gains `package_status` (default `"not_started"`),
  `package_error`, `package_path`, `packaged_at`.

## 6. Error handling

- Package prerequisites are strict: locked AND rendered. Manifest only
  needs locked (it validates numbering, not artifacts).
- A missing rendered artifact during zipping (GCS 404) fails the package
  with the document's Bates in the error — loud, not a silent hole.
- Continuity errors do NOT block packaging (they indicate a bug worth
  shipping visibly in the manifest, and the operator sees them in the
  on-demand manifest before packaging anyway).

## 7. Testing

- Pure round-trip (`test_loadfile_export.py`): `dat_bytes` output written to
  tmp_path and re-parsed with `parse_dat` — headers and values survive,
  BOM/þ/DC4/CRLF verified on raw bytes too; `opt_bytes` re-parsed with
  `parse_opt` — one doc per entry, path normalized; `check_continuity`
  catches gap, overlap, wrong end, wrong start; clean sequence returns [].
- Export assembly (`test_production_export.py`, fake-session): DAT row
  values incl. family attach ranges, withheld blanking, TEXTPATH gating,
  REDACTED/WITHHELD flags; `compute_manifest` counts/range/continuity;
  `package_set` happy path with monkeypatched storage (zip written, upload
  called, status packaged, artifact hashes present) and error path
  (missing artifact -> status error).
- Endpoints (append to `test_production_set_endpoints.py`): manifest 409 on
  draft, package 409s (not rendered / while packaging), worker delegation,
  package download 404/307.
- Migration purity + single head; full suite green (known `test_ai_review`
  failure excepted).

## Out of scope

- Per-page TIFF images + page-level OPT — with TIFF support.
- Produced-text OCR for redacted docs (re-OCR of burned images) — future;
  until then redacted docs ship without text by design.
- Builder/packaging UI — P2-4.
