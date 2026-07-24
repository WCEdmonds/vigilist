# Filetype Gaps — POTX, MBOX, RTF, ODT, ZIP Intake — Design + Plan

**Date:** 2026-07-23
**Status:** Approved (scope chosen by user from the support-matrix gap list, items 1–5)
**Branch:** `feat/filetype-gaps` (backend-only; runs parallel to the ontology-surfaces track — zero file overlap)

## Scope

Close five intake gaps. Explicitly OUT: legacy binary Office (DOC/XLS/PPT — prior deliberate cut, needs LibreOffice image change) and RAR (licensing; require pre-extraction).

| # | Format | Approach |
|---|---|---|
| 1 | POTX | Route to the existing PPTX extractor (same OOXML family) |
| 2 | MBOX | Stdlib `mailbox` splits the container; each message feeds the EXISTING eml parse path, inheriting threading/family/dedup behavior (mirror the PST-explode pattern) |
| 3 | RTF | Real text extraction via `striprtf` (new dep: pure-Python, tiny) instead of raw decode polluted with control words |
| 4 | ODT | Stdlib `zipfile` + XML text pull from `content.xml` (mirror DOCX extractor shape) |
| 5 | ZIP intake | Explode at ingest with guards; children become individual documents family-linked to the container, mirroring the email-attachment family convention |

**New dependency (backend): `striprtf`** — declared here; shows in the PR diff (requirements.txt).
**No schema changes, no migrations.** Existing columns (`family_id`, `extraction_status`, `file_type`) carry everything.

## Constraints

- `extract()` in `services/extractors.py` must NEVER raise (existing contract: bad file → error row).
- ZIP guards (hard limits, all violations → the zip ingests as a single unsupported/error document, never a crash): max nesting depth 2 (zip-in-zip once), max 500 entries per container, max 200MB per entry uncompressed, max 1GB total uncompressed, encrypted entries skipped (recorded in the container doc's extraction_error), path traversal (`..`) entries skipped.
- ZIP family convention: MIRROR the email-attachment convention exactly as implemented in `services/ingest_native.py` (read it; container doc + child docs share a family, container is the parent). Nested zips within depth: children attach to the OUTERMOST container's family.
- MBOX messages: each message flows through the same per-message pipeline as PST-exploded messages (hashing, threading headers, attachments) — no parallel implementation.
- Tests: fake/unit level per repo convention; for extractors use real tiny fixture bytes built in-test (e.g. `zipfile`/`mailbox` written to BytesIO), NOT mocks of the libraries. Known pre-existing suite failure `test_ai_review.py::test_build_classification_prompt` is not ours.

---

## Task F1: Extractor additions — POTX, ODT, RTF

**Files:** `backend/app/services/extractors.py`, `backend/requirements.txt` (+`striprtf`), test `backend/tests/test_extractors_new_formats.py`

Changes to `extractors.py`:
- Remove `.rtf` from `_TEXT_EXTS`; add explicit branch.
- In `extract()` routing, add:

```python
        if ext in (".pptx", ".potx"):
            t = _extract_pptx(data)
            return ExtractResult(t, ext.lstrip("."), _status_for(t))
        if ext == ".odt":
            t = _extract_odt(data)
            return ExtractResult(t, "odt", _status_for(t))
        if ext == ".rtf":
            t = _extract_rtf(data)
            return ExtractResult(t, "rtf", _status_for(t))
```
(Replacing the existing `.pptx`-only branch.) New extractors:

```python
def _extract_odt(data: bytes) -> str:
    import re
    import zipfile as _zipfile
    with _zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("content.xml").decode("utf-8", errors="replace")
    # <text:p>/<text:h> delimit paragraphs; strip all other tags.
    xml = re.sub(r"</text:(p|h)>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _extract_rtf(data: bytes) -> str:
    from striprtf.striprtf import rtf_to_text
    return rtf_to_text(data.decode("latin-1", errors="replace"), errors="ignore")
```

Tests (fixture bytes built in-test): a minimal ODT zipped in-memory (`mimetype` + `content.xml` with two `text:p` paragraphs) → both paragraphs extracted, no `<` in output; a POTX fixture is impractical to synthesize via python-pptx template quickly — instead assert routing: monkeypatch `_extract_pptx` and confirm `.potx` reaches it and reports `file_type == "potx"`; an RTF snippet `{\rtf1\ansi Hello {\b World}}` → "Hello World", no backslash control words; malformed bytes for each → `error` status, never raises; `.rtf` no longer routed through `_extract_text` (mutation-safe assertion: control words absent).

## Task F2: MBOX

**Files:** `backend/app/services/email_parse.py`, `backend/app/services/ingest_native.py`, test `backend/tests/test_mbox_parse.py`

- `email_parse.py`: add `_MBOX_EXTS = {".mbox"}`; `parse_mbox(data: bytes) -> list[ParsedEmail]` (or the module's actual per-message return type — READ the module and mirror `parse_pst`'s return contract exactly): write bytes to a temp file, iterate `mailbox.mbox(path)`, feed each message's bytes through the module's existing eml/message parsing function. Route `.mbox` in the module's container dispatch alongside PST.
- `ingest_native.py`: add `".mbox"` to `EMAIL_EXTS` and to the container set that triggers explode-to-messages (mirror `_PST_EXTS` handling — READ how PST containers flow and extend the same branches; message hashing note at ~line 273 applies identically: mbox messages are transient, hash message bytes).
- Tests: build a 2-message mbox in-memory via stdlib `mailbox.mbox` on a temp file (messages with Message-ID/In-Reply-To headers), run the parse, assert 2 parsed messages with correct subjects/headers flowing through the same shape PST tests use (READ tests/test_email_parse.py for the existing PST/eml test patterns and mirror them).

## Task F3: ZIP intake

**Files:** `backend/app/services/ingest_native.py` (+ possibly `services/extractors.py` for the container fallback), test `backend/tests/test_zip_intake.py`

Behavioral contract (implementer reads `ingest_native.py` first and mirrors the email-attachment implementation for family linking and doc creation):
- `.zip` upload → container Document row (native stored, `file_type="zip"`, no text, `extraction_status="ok"` if exploded cleanly else `"partial"`/`"error"` with reason) + one child Document per accepted entry, each processed through the NORMAL per-file pipeline (extension routing incl. PDFs/images/emails), all sharing the container's family per the email-attachment convention.
- Guards (spec Constraints section): depth ≤2, ≤500 entries, ≤200MB/entry, ≤1GB total, skip encrypted + path-traversal entries recording skips in the container's `extraction_error` (joined summary, capped 500 chars). Violating the aggregate limits mid-explode: keep already-created children, mark container `"partial"`.
- Nested zip within depth: recurse; its children join the OUTERMOST family; deeper zips ingest as unsupported single docs.
- Tests: in-memory zips via `zipfile` — happy path (2 files → 2 children + container, family linked); entry-count guard (501 entries → container error/partial per contract, no children beyond cap); traversal entry skipped; nested zip children in outer family; encrypted entry skipped with note. Mirror the module's existing test style (READ tests/test_ingest_native_email.py).

## Task F4: Final verification + PR

- Full backend suite; `pip install striprtf` into the shared venv before running (document in report).
- `git fetch origin main`, merge if moved, re-run.
- PR: "feat(ingest): filetype gaps — POTX, ODT, RTF, MBOX, ZIP intake", body summarizing the five formats, the one new dep, guard limits, and the family convention for zip children. Claude Code footer.

## Self-review notes

- POTX routing keeps `file_type="potx"` (distinct from pptx) so search filters can distinguish.
- RTF `errors="ignore"` on rtf_to_text guards malformed control sequences; outer try/except in extract() remains the last line of defense.
- ZIP explode runs at ingest (worker context), not request-time.
