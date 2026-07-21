# Phase 0 · SP4a — Loose-Native Ingest + Python Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `native` ingest mode that processes a folder of loose files (PDF/Office/text/image) with Python extractors, producing Documents with text, metadata, and SHA-256.

**Architecture:** A pure extraction dispatcher (`extractors.py`) routes a file's bytes to a per-format Python extractor; a native record processor (`ingest_native.py`) computes the hash, dispatches, and builds the Document (PDFs delegate to the existing `process_pdf_record`); `run_ingest_batch` gains a `native` branch and `/ingest/process` counts native sources + persists the per-upload custodian. The wizard gets a "Native files" mode.

**Tech Stack:** Python/FastAPI + SQLAlchemy async, python-docx/openpyxl/python-pptx + PyMuPDF + Cloud Vision (backend), React/TS/Vite (frontend), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-phase0-sp4a-native-extraction-design.md`.
- New pip deps in `backend/requirements.txt`: `python-docx`, `openpyxl`, `python-pptx`.
- `native` is a NEW `source_format`; no change to existing `generic_pdf`/`relativity` modes.
- Extractors NEVER raise — a bad/corrupt file → `extraction_status="error"` + `extraction_error`; the batch continues.
- `extraction_status ∈ {"ok", "partial", "unsupported", "error"}` (`partial` = supported type, empty text).
- PDFs delegate to the existing `process_pdf_record`; images + Office + text go through the dispatcher (images via the Vision `ocr_fn`, text-only, no page render).
- Per-upload custodian is stored in `IngestJob.field_mapping["custodian"]` (NO new migration) and stamped on every Document in the upload.
- Email containers (`.msg/.eml/.pst`) and legacy binary Office (`.doc/.xls/.ppt`) are `unsupported` here (email → SP4b).
- Backend tests deterministic, no network (Vision OCR injected via `ocr_fn`). Run backend tests from `backend/` with `python -m pytest`; frontend from `frontend/` with `npm run build`.

---

## File Structure
- `backend/app/services/extractors.py` *(new)* — extraction dispatcher + per-format extractors.
- `backend/tests/test_extractors.py` *(new)* — deterministic extractor tests.
- `backend/requirements.txt` — add three deps.
- `backend/app/services/ingest_native.py` *(new)* — `list_native_sources`, `process_native_record`, `ingest_native_batch`.
- `backend/app/services/ingest.py` — `run_ingest_batch` native branch.
- `backend/app/routers/ingest.py` — `/ingest/process` native counting + custodian persistence.
- `frontend/src/api/client.ts`, `frontend/src/components/IngestWizard.tsx` — native mode + custodian field.

---

## Task 1: Extraction dispatcher (`extractors.py`)

**Files:**
- Create: `backend/app/services/extractors.py`
- Modify: `backend/requirements.txt`
- Test: `backend/tests/test_extractors.py`

**Interfaces:**
- Produces: `@dataclass ExtractResult{ text: str, file_type: str, extraction_status: str, extraction_error: str | None }`; `extract(filename: str, data: bytes, ocr_fn=None) -> ExtractResult`.

- [ ] **Step 1: Add the dependencies**

Append to `backend/requirements.txt`:

```
python-docx>=1.1
openpyxl>=3.1
python-pptx>=0.6.23
```

Install locally: from `backend/`, `pip install python-docx openpyxl python-pptx` (or `venv/Scripts/python.exe -m pip install python-docx openpyxl python-pptx`).

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_extractors.py`:

```python
"""Unit tests for the loose-file extraction dispatcher."""

import io

from app.services.extractors import extract, ExtractResult


def _docx_bytes(text: str) -> bytes:
    from docx import Document as Docx
    d = Docx()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx_bytes(value: str) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.active["A1"] = value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pptx_bytes(text: str) -> bytes:
    from pptx import Presentation
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title-only layout
    slide.shapes.title.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_extract_docx():
    r = extract("a.docx", _docx_bytes("hello world"))
    assert r.extraction_status == "ok"
    assert "hello world" in r.text
    assert r.file_type == "docx"


def test_extract_xlsx():
    r = extract("b.xlsx", _xlsx_bytes("cell text"))
    assert r.extraction_status == "ok"
    assert "cell text" in r.text


def test_extract_pptx():
    r = extract("c.pptx", _pptx_bytes("slide title"))
    assert r.extraction_status == "ok"
    assert "slide title" in r.text


def test_extract_text_and_case_insensitive_ext():
    r = extract("notes.TXT", b"line one\nline two")
    assert r.extraction_status == "ok"
    assert "line one" in r.text


def test_extract_image_uses_ocr_fn():
    r = extract("scan.png", b"\x89PNG-not-real", ocr_fn=lambda b: "ocr text")
    assert r.extraction_status == "ok"
    assert r.text == "ocr text"
    assert r.file_type == "image"


def test_extract_unsupported():
    for name in ("old.doc", "mail.msg", "archive.pst", "weird.xyz", "noext"):
        r = extract(name, b"whatever")
        assert r.extraction_status == "unsupported", name
        assert r.text == ""


def test_extract_corrupt_supported_type_is_error():
    r = extract("broken.docx", b"not a real docx")
    assert r.extraction_status == "error"
    assert r.extraction_error
    assert r.text == ""
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_extractors.py -v` (from `backend/`)
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.extractors'`.

- [ ] **Step 4: Implement the dispatcher**

Create `backend/app/services/extractors.py`:

```python
"""Extract text from loose native files using Python libraries.

Pure and deterministic: no DB, no storage. Vision OCR for images is injected
via ``ocr_fn`` so callers/tests control it. Extraction never raises — parse
failures become an ``error`` result so the ingest batch can continue.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass


@dataclass
class ExtractResult:
    text: str
    file_type: str
    extraction_status: str            # ok | partial | unsupported | error
    extraction_error: str | None = None


_TEXT_EXTS = {".txt", ".csv", ".md", ".log", ".json", ".xml", ".html", ".htm", ".rtf"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp"}
# Extensions we deliberately do not handle here (email → SP4b; legacy binary Office).
_UNSUPPORTED_EXTS = {".doc", ".xls", ".ppt", ".msg", ".eml", ".pst"}


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _status_for(text: str) -> str:
    return "ok" if text.strip() else "partial"


def _extract_docx(data: bytes) -> str:
    from docx import Document as Docx
    doc = Docx(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text)
    return "\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append("\t".join(cells))
        return "\n".join(parts)
    finally:
        wb.close()


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    parts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                t = "".join(run.text for run in para.runs)
                if t.strip():
                    parts.append(t)
    return "\n".join(parts)


def _extract_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def extract(filename: str, data: bytes, ocr_fn=None) -> ExtractResult:
    """Route ``data`` to the extractor for ``filename``'s extension.

    NOTE: ``.pdf`` is intentionally NOT handled here — callers delegate PDFs to
    ``process_pdf_record`` (page render + OCR). A ``.pdf`` reaching this function
    is treated as unsupported.
    """
    ext = _ext(filename)
    ft = ext.lstrip(".") or "unknown"
    try:
        if ext == ".docx":
            t = _extract_docx(data)
            return ExtractResult(t, "docx", _status_for(t))
        if ext == ".xlsx":
            t = _extract_xlsx(data)
            return ExtractResult(t, "xlsx", _status_for(t))
        if ext == ".pptx":
            t = _extract_pptx(data)
            return ExtractResult(t, "pptx", _status_for(t))
        if ext in _TEXT_EXTS:
            t = _extract_text(data)
            return ExtractResult(t, ft, _status_for(t))
        if ext in _IMAGE_EXTS:
            t = (ocr_fn(data) if ocr_fn else "") or ""
            return ExtractResult(t, "image", _status_for(t))
        # Legacy Office, email, unknown/no extension.
        return ExtractResult("", ft, "unsupported")
    except Exception as e:  # never raise — a bad file is an error row, not a crash
        return ExtractResult("", ft, "error", str(e)[:500])
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/test_extractors.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/extractors.py backend/tests/test_extractors.py backend/requirements.txt
git commit -m "feat(ingest): Python extraction dispatcher for loose native files"
```

---

## Task 2: Native record processor (`ingest_native.py`)

**Files:**
- Create: `backend/app/services/ingest_native.py`

**Interfaces:**
- Consumes: `extract` (Task 1); `process_pdf_record`, `list_files`, `derive_bates_prefix`, `looks_like_bates_stub`, `_ocr_jpeg` (ingest_pdf); `get_download_bytes` (storage); the private batch helpers in `ingest.py` (`_persist_document`, `_incr_skipped`, `_persist_job_errors`, `_finalize_job_if_done`).
- Produces: `list_native_sources(production_id) -> list[dict]`; `process_native_record(custodian, production_id, item, global_index, prefix, errors) -> Document | None`; `async ingest_native_batch(db, job_id, production_id, start_idx, end_idx) -> None`.

- [ ] **Step 1: Create the module**

Create `backend/app/services/ingest_native.py`:

```python
"""Ingest a folder of loose native files (the `native` source_format).

Reuses the PDF page-render path for PDFs and the Python extraction dispatcher
for Office/text/image files. Per-file failures become error rows; the batch
never aborts on one bad file.
"""

import asyncio
import hashlib
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, IngestJob, Production
from app.services.extractors import extract
from app.services.storage import get_download_bytes, list_files

logger = logging.getLogger(__name__)


def list_native_sources(production_id: int) -> list[dict]:
    """List ALL uploaded files for a production, sorted deterministically.

    Mirrors ``list_pdf_sources`` but is not filtered by extension.
    """
    prefix = f"productions/{production_id}/raw/"
    all_files = sorted(list_files(prefix))
    items: list[dict] = []
    for path in all_files:
        relative_path = path[len(prefix):] if path.startswith(prefix) else path
        items.append(
            {
                "storage_path": path,
                "relative_path": relative_path,
                "filename": os.path.basename(relative_path),
            }
        )
    return items


def process_native_record(
    custodian: str | None,
    production_id: int,
    item: dict,
    global_index: int,
    prefix: str,
    errors: list[str],
) -> Document | None:
    """Turn one uploaded native file into an unsaved Document. Never raises."""
    from app.services.ingest_pdf import (
        _ocr_jpeg,
        looks_like_bates_stub,
        process_pdf_record,
    )

    control_number = f"{prefix} {global_index + 1:06d}"
    storage_path = item["storage_path"]
    relative_path = item["relative_path"]
    filename = item["filename"]
    ext = os.path.splitext(filename)[1].lower()

    try:
        data = get_download_bytes(storage_path)
    except Exception as e:
        errors.append(f"{control_number}: could not download {relative_path}: {e}")
        return None

    sha256 = hashlib.sha256(data).hexdigest()

    # PDFs: reuse the page-render + OCR path, then stamp SP4a fields.
    if ext == ".pdf":
        try:
            doc = process_pdf_record(production_id, item, global_index, prefix, errors)
        except Exception as e:
            errors.append(f"{control_number}: failed to render {relative_path}: {e}")
            return None
        if doc is None:
            return None
        doc.file_hash_sha256 = sha256
        doc.file_name = filename
        doc.file_type = "pdf"
        doc.source_path = relative_path
        doc.custodian = custodian
        return doc

    # Everything else: dispatch text extraction (images use Vision OCR).
    res = extract(filename, data, ocr_fn=_ocr_jpeg)

    folder = os.path.dirname(relative_path)
    metadata = {"File Name": filename}
    if folder:
        metadata["Folder"] = folder

    stem = os.path.splitext(filename)[0]
    title = None if looks_like_bates_stub(stem) else stem[:200]

    return Document(
        production_id=production_id,
        bates_begin=control_number,
        bates_end=control_number,
        page_count=1,
        metadata_=metadata,
        title=title,
        text_content=res.text or None,
        native_path=storage_path,
        image_paths=[],
        file_name=filename,
        file_type=res.file_type,
        source_path=relative_path,
        custodian=custodian,
        file_hash_sha256=sha256,
        extraction_status=res.extraction_status,
        extraction_error=res.extraction_error,
    )


async def ingest_native_batch(
    db: AsyncSession, job_id: str, production_id: int, start_idx: int, end_idx: int
) -> None:
    """Process one batch of native files. Mirrors ``ingest_pdf_batch``."""
    from app.services.ingest import (
        _finalize_job_if_done,
        _incr_skipped,
        _persist_document,
        _persist_job_errors,
    )
    from app.services.ingest_pdf import derive_bates_prefix

    job = await db.get(IngestJob, job_id)
    if not job:
        return
    production = await db.get(Production, production_id)
    prefix = derive_bates_prefix(production.name if production else "")
    custodian = (job.field_mapping or {}).get("custodian")

    items = list_native_sources(production_id)
    errors: list[str] = list(job.errors or [])

    slice_pairs = [(idx, items[idx]) for idx in range(start_idx, min(end_idx, len(items)))]
    storage_paths = [item["storage_path"] for _, item in slice_pairs]
    existing: set[str] = set()
    if storage_paths:
        result = await db.execute(
            select(Document.native_path).where(
                Document.production_id == production_id,
                Document.native_path.in_(storage_paths),
            )
        )
        existing = {row[0] for row in result.all()}

    for global_index, item in slice_pairs:
        control_number = f"{prefix} {global_index + 1:06d}"
        if item["storage_path"] in existing:
            await _incr_skipped(db, job_id)
            continue
        try:
            doc = await asyncio.to_thread(
                process_native_record,
                custodian, production_id, item, global_index, prefix, errors,
            )
            if doc is None:
                await _incr_skipped(db, job_id)
                continue
            await _persist_document(db, job_id, doc)
        except Exception as e:
            logger.exception("Failed to process native file %s", item.get("relative_path"))
            errors.append(f"{control_number}: {e}")
            await db.rollback()
            await _incr_skipped(db, job_id)

    await _persist_job_errors(db, job_id, errors)
    await _finalize_job_if_done(db, job, production_id, errors)
```

- [ ] **Step 2: Verify import + no regression**

Run (from `backend/`): `python -c "import app.services.ingest_native"` → no ImportError.
Run: `python -m pytest -q` → no NEW failures (pre-existing `test_ai_review.py::test_build_classification_prompt` may remain).

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/ingest_native.py
git commit -m "feat(ingest): native record processor + batch (loose files)"
```

---

## Task 3: Wiring (`run_ingest_batch` + `/ingest/process`)

**Files:**
- Modify: `backend/app/services/ingest.py` (`run_ingest_batch`)
- Modify: `backend/app/routers/ingest.py` (`start_processing`)

**Interfaces:**
- Consumes: `ingest_native_batch`, `list_native_sources` (Task 2).

- [ ] **Step 1: Dispatch native batches**

In `backend/app/services/ingest.py`, `run_ingest_batch` currently reads:

```python
    job = await db.get(IngestJob, job_id)
    if job and job.source_format == "generic_pdf":
        await ingest_pdf_batch(db, job_id, production_id, start_idx, end_idx)
    else:
        await ingest_batch(db, job_id, production_id, start_idx, end_idx)
```

Replace with:

```python
    job = await db.get(IngestJob, job_id)
    if job and job.source_format == "generic_pdf":
        await ingest_pdf_batch(db, job_id, production_id, start_idx, end_idx)
    elif job and job.source_format == "native":
        from app.services.ingest_native import ingest_native_batch
        await ingest_native_batch(db, job_id, production_id, start_idx, end_idx)
    else:
        await ingest_batch(db, job_id, production_id, start_idx, end_idx)
```

- [ ] **Step 2: Count native sources + persist custodian in `/ingest/process`**

In `backend/app/routers/ingest.py`, `start_processing`:

(a) After `field_mapping = body.get("field_mapping") or {}` (currently line ~134), add native custodian handling and widen the batch size:

```python
    field_mapping = body.get("field_mapping") or {}
    if source_format == "native":
        field_mapping = {"custodian": (body.get("custodian") or "").strip() or None}
    batch_size = 10 if source_format in ("generic_pdf", "native") else INGEST_BATCH_SIZE
```

(b) In the Cloud-Tasks counting block, add a `native` branch alongside `generic_pdf`:

```python
            if source_format == "generic_pdf":
                from app.services.ingest_pdf import list_pdf_sources
                total_files = len(list_pdf_sources(production.id))
            elif source_format == "native":
                from app.services.ingest_native import list_native_sources
                total_files = len(list_native_sources(production.id))
            else:
                records, _ = bootstrap_ingest_source(production.id)
                total_files = len(records)
```

The `IngestJob(...)` construction already passes `field_mapping=field_mapping` (SP1), so the custodian persists on both the Cloud-Tasks and inline-fallback job creations — confirm both `IngestJob(...)` calls include `field_mapping=field_mapping` and leave them as-is.

- [ ] **Step 3: Verify**

Run (from `backend/`): `python -c "import app.routers.ingest; import app.services.ingest"` → no ImportError.
Run: `python -m pytest -q` → no NEW failures.

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/ingest.py backend/app/routers/ingest.py
git commit -m "feat(ingest): wire native source_format (batch dispatch + count + custodian)"
```

---

## Task 4: Frontend — Native files mode + custodian

**Files:**
- Modify: `frontend/src/api/client.ts` (`startProcessing`)
- Modify: `frontend/src/components/IngestWizard.tsx`

**Interfaces:**
- Consumes: `/ingest/process` with `source_format: "native"` + `custodian`.

- [ ] **Step 1: Widen `startProcessing`**

In `frontend/src/api/client.ts`, replace `startProcessing` with a version that accepts `native` and an optional custodian:

```typescript
export const startProcessing = (
  productionId: number,
  totalFiles: number,
  sourceFormat: 'relativity' | 'generic_pdf' | 'native' = 'relativity',
  fieldMapping: Record<string, string> = {},
  custodian: string = '',
) =>
  request<IngestJob>('/api/ingest/process', json({
    production_id: productionId,
    total_files: totalFiles,
    source_format: sourceFormat,
    field_mapping: fieldMapping,
    custodian,
  }));
```

(Keep the existing `json`/`request` helpers. The added `custodian` is ignored by the backend for non-native modes.)

- [ ] **Step 2: Add the native mode + custodian field to the wizard**

In `frontend/src/components/IngestWizard.tsx`:

- Widen the mode state type everywhere it appears (declaration + `chooseMode` param + the auto-detect var) from `'relativity' | 'generic_pdf'` to `'relativity' | 'generic_pdf' | 'native'`.
- Add custodian state near the other `useState`s: `const [custodian, setCustodian] = useState('');`
- In the mode-selection UI, add a third choice button for `'native'` labeled "Native files" (mirror the existing generic_pdf button's markup/handler, calling `chooseMode('native')`).
- Render a custodian text input, shown only when `mode === 'native'`, e.g. below the mode buttons:

```tsx
{mode === 'native' && (
  <label style={{ display: 'block', marginTop: 8 }}>
    <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>Custodian (optional)</span>
    <input className="input" value={custodian} onChange={e => setCustodian(e.target.value)} placeholder="e.g. Jane Smith" />
  </label>
)}
```

- In the upload handler, the existing `uploadList` already uploads all files for non-`generic_pdf` modes, so `native` uploads everything with no change. Update the non-relativity process call (currently `startProcessing(production_id, uploadList.length, mode)`) to pass the custodian:

```tsx
        const ingestJob = await startProcessing(production_id, uploadList.length, mode, {}, custodian);
```

- [ ] **Step 3: Build + lint**

Run (from `frontend/`): `npm run build` → 0 type errors.
Run: `npx eslint src/components/IngestWizard.tsx src/api/client.ts` → no NEW errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/components/IngestWizard.tsx
git commit -m "feat(ingest): Native files wizard mode + custodian field"
```

---

## Self-Review

**Spec coverage:** extraction dispatcher w/ per-format extractors + status model + deps (Task 1) ✓; `list_native_sources` + `process_native_record` (hash, PDF-delegate, direct build, never-raise) + `ingest_native_batch` (Task 2) ✓; `run_ingest_batch` native branch + `/ingest/process` count + custodian persistence + batch size (Task 3) ✓; wizard native mode + custodian field + all-file upload + process call (Task 4) ✓; email/PST + legacy Office unsupported, existing modes unchanged — enforced by routing/constraints ✓.

**Placeholder scan:** No TBD/TODO; every code step has complete code.

**Type consistency:** `ExtractResult`/`extract(filename, data, ocr_fn)` (Task 1) consumed by `process_native_record` (Task 2). `process_native_record(custodian, production_id, item, global_index, prefix, errors)` + `ingest_native_batch(db, job_id, production_id, start_idx, end_idx)` (Task 2) consumed by wiring (Task 3). `source_format="native"` literal + `field_mapping["custodian"]` consistent across backend (Tasks 2–3) and frontend (Task 4). `startProcessing(..., custodian)` (Task 4) matches the `/ingest/process` body key `custodian` (Task 3).

**Notes for reviewer:** `ingest_native_batch` lazily imports the private batch helpers from `ingest.py` inside the function to avoid a circular import (ingest.py → ingest_native at call time). The DB/storage-bound processor + wiring have no unit tests (per the repo's no-DB convention); the extraction logic carries the unit tests (Task 1). No migration.
