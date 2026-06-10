# Generic PDF Folder Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users ingest a folder of loose PDFs (with arbitrary subfolders) that has no Relativity load files, alongside the existing Relativity pipeline.

**Architecture:** Reuse the existing upload → `IngestJob` → Cloud Tasks fan-out → status-poll machinery. A new `source_format` flag on `IngestJob` selects the per-item processor. For generic PDF, "a record" is a PDF file: PyMuPDF renders each page to a 250-DPI JPEG and extracts the embedded text layer (Cloud Vision OCR fallback for scanned pages), each PDF becomes one `Document` with a synthetic control number, its filename as title, and its subfolder path stored as metadata.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async, PyMuPDF (new), Alembic, Firebase Storage, Cloud Tasks, Google Cloud Vision; React + TypeScript frontend.

**Spec:** `docs/superpowers/specs/2026-06-09-generic-pdf-ingest-design.md`

---

## File Structure

- **Create** `backend/app/services/ingest_pdf.py` — all generic-PDF logic: prefix derivation, source listing, the pure render/extract core, and the storage-backed `process_pdf_record`.
- **Create** `backend/tests/test_ingest_pdf.py` — unit tests for the pure functions.
- **Modify** `backend/requirements.txt` — add `pymupdf`.
- **Modify** `backend/app/models.py` — add `source_format` column to `IngestJob`.
- **Create** `backend/alembic/versions/j3e8f7g26h59_add_source_format_to_ingest_jobs.py` — migration.
- **Modify** `backend/app/services/ingest.py` — extract shared job-bookkeeping helpers; add `ingest_pdf_batch`; dispatch by `source_format`.
- **Modify** `backend/app/routers/ingest.py` — `/ingest/process` counts PDFs and sets `source_format` for generic jobs.
- **Modify** `frontend/src/api/client.ts` — `startProcessing` forwards `source_format`.
- **Modify** `frontend/src/components/IngestWizard.tsx` — mode toggle, auto-detect, PDF-only validation + upload.

---

## Task 1: Add the PyMuPDF dependency

**Files:**
- Modify: `backend/requirements.txt`

- [ ] **Step 1: Add the dependency**

Append this line to `backend/requirements.txt` (after the `datasketch>=1.6` line):

```
pymupdf>=1.24
```

- [ ] **Step 2: Install it**

Run (from `backend/`, with the venv active):
```
pip install "pymupdf>=1.24"
```
Expected: `Successfully installed pymupdf-...`

- [ ] **Step 3: Verify the import works**

Run:
```
python -c "import fitz; print(fitz.__doc__.splitlines()[0])"
```
Expected: a PyMuPDF/MuPDF version banner line, no `ImportError`.

- [ ] **Step 4: Commit**

```bash
git add backend/requirements.txt
git commit -m "build: add pymupdf for generic PDF ingest"
```

---

## Task 2: Derive the Bates prefix from the production name

**Files:**
- Create: `backend/app/services/ingest_pdf.py`
- Test: `backend/tests/test_ingest_pdf.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ingest_pdf.py`:

```python
from app.services.ingest_pdf import derive_bates_prefix


def test_prefix_uppercases_first_token():
    assert derive_bates_prefix("Smith Loose Docs") == "SMITH"


def test_prefix_strips_punctuation():
    assert derive_bates_prefix("smith-jones, llp") == "SMITHJONES"


def test_prefix_truncates_to_12_chars():
    assert derive_bates_prefix("Supercalifragilistic Matter") == "SUPERCALIFRA"


def test_prefix_falls_back_to_doc_when_empty():
    assert derive_bates_prefix("!!! ???") == "DOC"
    assert derive_bates_prefix("") == "DOC"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ingest_pdf.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.ingest_pdf'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/ingest_pdf.py`:

```python
"""Generic PDF folder ingest — no Relativity load files required.

Each PDF becomes one Document: pages are rendered to JPEGs via PyMuPDF
and the embedded text layer is extracted, with a Cloud Vision OCR
fallback for scanned pages. Documents get a synthetic control number
in place of a Bates number.
"""

import logging
import re

logger = logging.getLogger(__name__)

RENDER_DPI = 250
# A page with fewer than this many non-whitespace characters of embedded
# text is treated as scanned and sent to OCR.
MIN_TEXT_CHARS = 10


def derive_bates_prefix(production_name: str) -> str:
    """Derive a Bates-style prefix from a production name.

    Uppercase, strip everything but A-Z/0-9/space, collapse whitespace,
    take the first token, truncate to 12 chars. Falls back to "DOC".
    """
    cleaned = re.sub(r"[^A-Z0-9 ]", "", (production_name or "").upper())
    tokens = cleaned.split()
    if not tokens:
        return "DOC"
    return tokens[0][:12]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ingest_pdf.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ingest_pdf.py backend/tests/test_ingest_pdf.py
git commit -m "feat: derive Bates prefix from production name for PDF ingest"
```

---

## Task 3: Render PDF pages and extract text (pure core)

This is the unit-testable heart of the feature. It takes raw PDF bytes and an
injected OCR function (so tests need no network), and returns one JPEG per page,
the combined text, and the page count.

**Files:**
- Modify: `backend/app/services/ingest_pdf.py`
- Test: `backend/tests/test_ingest_pdf.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_ingest_pdf.py` (top imports + new tests):

```python
import fitz  # PyMuPDF

from app.services.ingest_pdf import render_and_extract_pdf


def _born_digital_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _blank_two_page_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_born_digital_uses_embedded_text_and_skips_ocr():
    ocr_calls = []

    def fake_ocr(jpeg_bytes: bytes) -> str:
        ocr_calls.append(jpeg_bytes)
        return "SHOULD-NOT-BE-USED"

    pages, text, page_count = render_and_extract_pdf(
        _born_digital_pdf("Hello discovery"), ocr_fn=fake_ocr
    )

    assert page_count == 1
    assert len(pages) == 1
    assert pages[0][:3] == b"\xff\xd8\xff"  # JPEG magic bytes
    assert "Hello discovery" in text
    assert ocr_calls == []  # OCR not invoked for born-digital text


def test_scanned_page_falls_back_to_ocr():
    def fake_ocr(jpeg_bytes: bytes) -> str:
        return "OCR-RECOVERED-TEXT"

    pages, text, page_count = render_and_extract_pdf(
        _blank_two_page_pdf(), ocr_fn=fake_ocr
    )

    assert page_count == 2
    assert len(pages) == 2
    assert text.count("OCR-RECOVERED-TEXT") == 2


def test_pages_rendered_for_every_page():
    pages, _text, page_count = render_and_extract_pdf(
        _blank_two_page_pdf(), ocr_fn=lambda b: ""
    )
    assert page_count == len(pages) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ingest_pdf.py -k render_and_extract -v`
Expected: FAIL with `ImportError: cannot import name 'render_and_extract_pdf'`

- [ ] **Step 3: Write minimal implementation**

Add to `backend/app/services/ingest_pdf.py`:

```python
from typing import Callable

import fitz  # PyMuPDF


def render_and_extract_pdf(
    pdf_bytes: bytes,
    ocr_fn: Callable[[bytes], str],
    dpi: int = RENDER_DPI,
) -> tuple[list[bytes], str, int]:
    """Render every page to a JPEG and extract its text.

    Returns (jpeg_bytes_per_page, combined_text, page_count). Uses the
    embedded text layer when present; calls ocr_fn(jpeg_bytes) for pages
    whose embedded text is empty/sparse (scanned pages).
    """
    jpeg_pages: list[bytes] = []
    text_parts: list[str] = []

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            jpeg = pix.tobytes("jpeg")
            jpeg_pages.append(jpeg)

            embedded = page.get_text().strip()
            if len(embedded.replace(" ", "").replace("\n", "")) >= MIN_TEXT_CHARS:
                text_parts.append(embedded)
            else:
                ocr_text = ocr_fn(jpeg) or ""
                if ocr_text.strip():
                    text_parts.append(ocr_text.strip())

        page_count = doc.page_count
    finally:
        doc.close()

    return jpeg_pages, "\n\n".join(text_parts), page_count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ingest_pdf.py -v`
Expected: PASS (all tests, including Task 2's)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ingest_pdf.py backend/tests/test_ingest_pdf.py
git commit -m "feat: render PDF pages to JPEG and extract text with OCR fallback"
```

---

## Task 4: List PDF sources and build a Document from one PDF

`list_pdf_sources` is the generic-PDF analog of `bootstrap_ingest_source`:
it returns a deterministically sorted list of uploaded PDFs. `process_pdf_record`
downloads one PDF, runs the pure core, uploads the page JPEGs, and assembles a
`Document`. The control number comes from the file's global index, so retried
batches reproduce the same `bates_begin`.

**Files:**
- Modify: `backend/app/services/ingest_pdf.py`
- Test: `backend/tests/test_ingest_pdf.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_ingest_pdf.py`:

```python
from app.services import ingest_pdf as pdf_mod
from app.services.ingest_pdf import list_pdf_sources, process_pdf_record


def test_list_pdf_sources_sorts_and_keeps_relative_path(monkeypatch):
    raw = [
        "productions/7/raw/B/second.pdf",
        "productions/7/raw/A/first.PDF",
        "productions/7/raw/notes.txt",
        "productions/7/raw/A/skip.opt",
    ]
    monkeypatch.setattr(pdf_mod, "list_files", lambda prefix: raw)

    items = list_pdf_sources(7)

    # Only PDFs, case-insensitive, sorted by storage path
    assert [i["storage_path"] for i in items] == [
        "productions/7/raw/A/first.PDF",
        "productions/7/raw/B/second.pdf",
    ]
    assert items[0]["relative_path"] == "A/first.PDF"
    assert items[0]["filename"] == "first.PDF"


def test_process_pdf_record_assembles_document(monkeypatch):
    item = {
        "storage_path": "productions/7/raw/A/first.pdf",
        "relative_path": "A/first.pdf",
        "filename": "first.pdf",
    }

    monkeypatch.setattr(pdf_mod, "get_download_bytes", lambda path: b"%PDF-fake")
    monkeypatch.setattr(
        pdf_mod,
        "render_and_extract_pdf",
        lambda pdf_bytes, ocr_fn, dpi=pdf_mod.RENDER_DPI: (
            [b"\xff\xd8jpeg1", b"\xff\xd8jpeg2"],
            "extracted text",
            2,
        ),
    )
    uploaded = []
    monkeypatch.setattr(
        pdf_mod,
        "upload_bytes",
        lambda data, remote, content_type=None: uploaded.append(remote) or remote,
    )

    errors: list[str] = []
    doc = process_pdf_record(
        production_id=7,
        item=item,
        global_index=0,
        prefix="SMITH",
        errors=errors,
    )

    assert doc.bates_begin == "SMITH 000001"
    assert doc.bates_end == "SMITH 000001"
    assert doc.page_count == 2
    assert doc.title == "first"
    assert doc.text_content == "extracted text"
    assert doc.metadata_["File Name"] == "first.pdf"
    assert doc.metadata_["Folder"] == "A"
    assert doc.native_path == "productions/7/raw/A/first.pdf"
    assert len(doc.image_paths) == 2
    assert len(uploaded) == 2
    assert errors == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ingest_pdf.py -k "list_pdf_sources or process_pdf_record" -v`
Expected: FAIL with `ImportError: cannot import name 'list_pdf_sources'`

- [ ] **Step 3: Add `upload_bytes` to the storage service**

The storage service only has `upload_file` (from a local path). Add a
bytes-based uploader. Add to `backend/app/services/storage.py` after
`upload_file`:

```python
def upload_bytes(data: bytes, remote_path: str, content_type: str | None = None) -> str:
    """Upload raw bytes to Firebase Storage. Returns the remote path."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    blob.upload_from_string(data, content_type=content_type)
    return remote_path
```

- [ ] **Step 4: Write the implementation**

Add to `backend/app/services/ingest_pdf.py`:

```python
import os

from app.models import Document
from app.services.storage import get_download_bytes, list_files, upload_bytes


def list_pdf_sources(production_id: int) -> list[dict]:
    """List uploaded PDFs for a production, sorted deterministically.

    Returns a list of {storage_path, relative_path, filename} dicts.
    Slice indices into this list match across calls (sorted), so batch
    workers and retries process the same items.
    """
    prefix = f"productions/{production_id}/raw/"
    all_files = list_files(prefix)
    pdfs = [f for f in all_files if f.lower().endswith(".pdf")]
    pdfs.sort()

    items: list[dict] = []
    for path in pdfs:
        relative_path = path[len(prefix):] if path.startswith(prefix) else path
        items.append(
            {
                "storage_path": path,
                "relative_path": relative_path,
                "filename": os.path.basename(relative_path),
            }
        )
    return items


def _ocr_jpeg(jpeg_bytes: bytes) -> str:
    """OCR a single rendered page via Cloud Vision. Best-effort."""
    try:
        from app.services.ocr import ocr_image_vision_bytes

        return ocr_image_vision_bytes(jpeg_bytes)
    except Exception:
        logger.exception("Vision OCR failed for a rendered PDF page")
        return ""


def process_pdf_record(
    production_id: int,
    item: dict,
    global_index: int,
    prefix: str,
    errors: list[str],
) -> Document | None:
    """Turn one uploaded PDF into an unsaved Document.

    `global_index` is the file's 0-based position in the full sorted
    source list; the control number is derived from it so retried
    batches reproduce the same bates_begin.
    """
    control_number = f"{prefix} {global_index + 1:06d}"
    storage_path = item["storage_path"]
    relative_path = item["relative_path"]
    filename = item["filename"]

    try:
        pdf_bytes = get_download_bytes(storage_path)
    except Exception as e:
        errors.append(f"{control_number}: could not download {relative_path}: {e}")
        return None

    try:
        jpeg_pages, text_content, page_count = render_and_extract_pdf(
            pdf_bytes, ocr_fn=_ocr_jpeg
        )
    except Exception as e:
        errors.append(f"{control_number}: failed to render {relative_path}: {e}")
        return None

    image_paths: list[str] = []
    stem = os.path.splitext(filename)[0]
    for page_num, jpeg in enumerate(jpeg_pages, start=1):
        remote = (
            f"productions/{production_id}/converted/"
            f"{control_number.replace(' ', '_')}_{page_num:04d}.jpg"
        )
        try:
            upload_bytes(jpeg, remote, content_type="image/jpeg")
            image_paths.append(remote)
        except Exception as e:
            errors.append(f"{control_number}: image upload failed page {page_num}: {e}")
            image_paths.append("")

    folder = os.path.dirname(relative_path)
    metadata = {"File Name": filename}
    if folder:
        metadata["Folder"] = folder

    return Document(
        production_id=production_id,
        bates_begin=control_number,
        bates_end=control_number,
        page_count=page_count or 1,
        metadata_=metadata,
        title=stem[:200],
        text_content=text_content or None,
        native_path=storage_path,
        image_paths=image_paths,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_ingest_pdf.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/ingest_pdf.py backend/app/services/storage.py backend/tests/test_ingest_pdf.py
git commit -m "feat: list PDF sources and build Documents from PDFs"
```

---

## Task 5: Add `source_format` to the IngestJob model + migration

**Files:**
- Modify: `backend/app/models.py` (the `IngestJob` class, around lines 198-213)
- Create: `backend/alembic/versions/j3e8f7g26h59_add_source_format_to_ingest_jobs.py`

- [ ] **Step 1: Add the column to the model**

In `backend/app/models.py`, inside `class IngestJob`, add the column right after
the `status` column:

```python
    status = Column(String(20), nullable=False, default="pending")
    source_format = Column(String(20), nullable=False, server_default="relativity")
```

- [ ] **Step 2: Write the migration**

Create `backend/alembic/versions/j3e8f7g26h59_add_source_format_to_ingest_jobs.py`:

```python
"""add source_format to ingest_jobs

Revision ID: j3e8f7g26h59
Revises: i2d7e6f15g48
Create Date: 2026-06-09

"""
from alembic import op
import sqlalchemy as sa

revision = "j3e8f7g26h59"
down_revision = "i2d7e6f15g48"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingest_jobs",
        sa.Column(
            "source_format",
            sa.String(length=20),
            nullable=False,
            server_default="relativity",
        ),
    )


def downgrade() -> None:
    op.drop_column("ingest_jobs", "source_format")
```

- [ ] **Step 3: Apply the migration**

Run (from `backend/`, venv active, Postgres up via `docker compose up -d`):
```
alembic upgrade head
```
Expected: `Running upgrade i2d7e6f15g48 -> j3e8f7g26h59, add source_format to ingest_jobs`

- [ ] **Step 4: Verify the column exists**

Run:
```
python -c "import asyncio; from sqlalchemy import text; from app.database import async_session_factory; \
asyncio.run((lambda: None)()) or asyncio.run(__import__('app.database', fromlist=['x']) and (lambda: None)())" 2>/dev/null; \
psql -h localhost -U postgres -d vigilist -c "\d ingest_jobs" 2>/dev/null | grep source_format || echo "check manually"
```
Expected: a `source_format | character varying(20)` row, OR if `psql` isn't on
PATH, confirm via the app starting without error in a later task. (Do not block
on this step if `psql` is unavailable — the `alembic upgrade head` success in
Step 3 is the authoritative check.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/j3e8f7g26h59_add_source_format_to_ingest_jobs.py
git commit -m "feat: add source_format column to ingest_jobs"
```

---

## Task 6: Extract shared bookkeeping helpers (no behavior change)

`ingest_batch` in `ingest.py` (lines ~294-454) mixes per-item DB bookkeeping
(increment skipped/processed, finalize the job) with the Relativity-specific
loop. Extract the bookkeeping so the PDF batch worker in Task 7 can reuse it.
This task is a pure refactor — the Relativity path must stay green.

**Files:**
- Modify: `backend/app/services/ingest.py`

- [ ] **Step 1: Add the helper functions**

Add these module-level helpers to `backend/app/services/ingest.py`, just above
`async def ingest_batch` (after the `INGEST_BATCH_SIZE` / `process_ingest_record`
definitions):

```python
async def _incr_skipped(db: AsyncSession, job_id: str) -> None:
    """Count one record as skipped."""
    await db.execute(
        text("UPDATE ingest_jobs SET skipped_files = skipped_files + 1 WHERE id = :jid"),
        {"jid": job_id},
    )
    await db.commit()


async def _persist_document(db: AsyncSession, job_id: str, doc: Document) -> None:
    """Persist a freshly built Document: flush, set tsvector + status, bump progress."""
    db.add(doc)
    await db.flush()
    await db.execute(
        text(
            "UPDATE documents SET text_search_vector = "
            "to_tsvector('english', COALESCE(text_content, '')), "
            "processing_status = 'complete' "
            "WHERE id = :id"
        ),
        {"id": doc.id},
    )
    await db.execute(
        text("UPDATE ingest_jobs SET processed_files = processed_files + 1 WHERE id = :jid"),
        {"jid": job_id},
    )
    await db.commit()


async def _finalize_job_if_done(
    db: AsyncSession,
    job: "IngestJob",
    production_id: int,
    errors: list[str],
) -> None:
    """Finalize the job (AI titles + mark complete) once all files are accounted for."""
    from datetime import datetime, timezone

    await db.refresh(job)
    if (job.processed_files + job.skipped_files) >= job.total_files and job.status == "processing":
        if settings.anthropic_api_key:
            try:
                result = await db.execute(
                    select(Document).where(
                        Document.production_id == production_id,
                        Document.title.is_(None),
                    )
                )
                docs_for_titles = list(result.scalars().all())
                texts_for_titles = [(str(d.id), d.text_content) for d in docs_for_titles]
                titles = await generate_titles_batch(texts_for_titles)
                for d in docs_for_titles:
                    t = titles.get(str(d.id))
                    if t:
                        d.title = t
                await db.commit()
            except Exception as e:
                logger.exception("AI title generation failed")
                errors.append(f"AI title generation skipped: {e}")

        job.status = "complete"
        job.errors = errors
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
```

- [ ] **Step 2: Use the helpers inside `ingest_batch`**

In `ingest_batch`, replace each inline skipped-increment block with
`await _incr_skipped(db, job_id)`, replace the success block (add/flush/tsvector/
processed-increment) with `await _persist_document(db, job_id, doc)`, and replace
the finalize block (lines ~424-452) with
`await _finalize_job_if_done(db, job, production_id, errors)`.

The body of the per-record loop becomes:

```python
        for record in slice_records:
            bates_begin = record.get("Begin Bates", "").strip()
            if not bates_begin:
                errors.append("Row: missing Begin Bates")
                await _incr_skipped(db, job_id)
                continue
            if bates_begin in existing:
                await _incr_skipped(db, job_id)
                continue
            try:
                doc = process_ingest_record(
                    production_id, record, opt_pages, converted_tmp, errors
                )
                if doc is None:
                    await _incr_skipped(db, job_id)
                    continue
                await _persist_document(db, job_id, doc)
            except Exception as e:
                logger.exception("Failed to process record %s", bates_begin)
                errors.append(f"{bates_begin}: {e}")
                await db.rollback()
                await _incr_skipped(db, job_id)

        # Persist any error messages collected in this batch
        await db.execute(
            text("UPDATE ingest_jobs SET errors = :errs::jsonb WHERE id = :jid"),
            {"errs": json.dumps(errors), "jid": job_id},
        )
        await db.commit()

        await _finalize_job_if_done(db, job, production_id, errors)
```

Leave the `tmp_dir` setup/`finally: shutil.rmtree(...)` exactly as-is.

- [ ] **Step 3: Verify nothing is broken (import + existing tests)**

Run (from `backend/`):
```
python -c "import app.services.ingest"
pytest -q
```
Expected: import succeeds; existing test suite passes (no new failures).

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/ingest.py
git commit -m "refactor: extract shared ingest job bookkeeping helpers"
```

---

## Task 7: Add the PDF batch worker and dispatch by source_format

**Files:**
- Modify: `backend/app/services/ingest.py`

- [ ] **Step 1: Add `ingest_pdf_batch`**

Add to `backend/app/services/ingest.py` (after `ingest_batch`):

```python
async def ingest_pdf_batch(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    start_idx: int,
    end_idx: int,
) -> None:
    """Process PDFs[start_idx:end_idx] for a generic-PDF ingest job.

    Idempotent: documents already present (by production_id + control
    number) are skipped, so retried batches are safe.
    """
    from app.models import IngestJob, Production
    from app.services.ingest_pdf import (
        derive_bates_prefix,
        list_pdf_sources,
        process_pdf_record,
    )

    job = await db.get(IngestJob, job_id)
    if not job:
        return
    production = await db.get(Production, production_id)
    prefix = derive_bates_prefix(production.name if production else "")

    items = list_pdf_sources(production_id)
    errors: list[str] = list(job.errors or [])

    # Control numbers for this slice, by global index
    slice_pairs = [
        (idx, items[idx]) for idx in range(start_idx, min(end_idx, len(items)))
    ]
    control_numbers = [f"{prefix} {idx + 1:06d}" for idx, _ in slice_pairs]

    existing: set[str] = set()
    if control_numbers:
        result = await db.execute(
            select(Document.bates_begin).where(
                Document.production_id == production_id,
                Document.bates_begin.in_(control_numbers),
            )
        )
        existing = {row[0] for row in result.all()}

    for global_index, item in slice_pairs:
        control_number = f"{prefix} {global_index + 1:06d}"
        if control_number in existing:
            await _incr_skipped(db, job_id)
            continue
        try:
            doc = process_pdf_record(
                production_id, item, global_index, prefix, errors
            )
            if doc is None:
                await _incr_skipped(db, job_id)
                continue
            await _persist_document(db, job_id, doc)
        except Exception as e:
            logger.exception("Failed to process PDF %s", item.get("relative_path"))
            errors.append(f"{control_number}: {e}")
            await db.rollback()
            await _incr_skipped(db, job_id)

    await db.execute(
        text("UPDATE ingest_jobs SET errors = :errs::jsonb WHERE id = :jid"),
        {"errs": json.dumps(errors), "jid": job_id},
    )
    await db.commit()

    await _finalize_job_if_done(db, job, production_id, errors)
```

- [ ] **Step 2: Dispatch by source_format in the batch entry points**

The Cloud Tasks worker calls `ingest_batch` and the inline fallback loops over
`ingest_batch`. Add a dispatcher so both honor `source_format`.

Add this function after `ingest_pdf_batch`:

```python
async def run_ingest_batch(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    start_idx: int,
    end_idx: int,
) -> None:
    """Dispatch one batch to the right processor based on job.source_format."""
    from app.models import IngestJob

    job = await db.get(IngestJob, job_id)
    if job and job.source_format == "generic_pdf":
        await ingest_pdf_batch(db, job_id, production_id, start_idx, end_idx)
    else:
        await ingest_batch(db, job_id, production_id, start_idx, end_idx)
```

- [ ] **Step 3: Point the Cloud Tasks worker at the dispatcher**

In `backend/app/routers/ingest.py`, in `process_batch_handler`, change the import
and call from `ingest_batch` to `run_ingest_batch`:

```python
    from app.services.ingest import run_ingest_batch
    ...
    try:
        await run_ingest_batch(db, job_id, int(production_id), int(start_idx), int(end_idx))
    except Exception as e:
        logger.exception("Ingest batch failed")
        raise HTTPException(status_code=500, detail=f"Ingest batch failed: {e}")
```

- [ ] **Step 4: Point the inline fallback at the dispatcher**

In `backend/app/services/ingest.py`, `ingest_from_storage` currently loops over
`ingest_batch` and always parses DAT/OPT via `bootstrap_ingest_source`. Make it
format-aware:

```python
async def ingest_from_storage(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    production_name: str,
) -> None:
    """In-process fallback ingest used when Cloud Tasks isn't configured."""
    from datetime import datetime, timezone

    from app.models import IngestJob
    from app.services.ingest_pdf import list_pdf_sources

    job = await db.get(IngestJob, job_id)
    if not job:
        return

    try:
        if job.source_format == "generic_pdf":
            total = len(list_pdf_sources(production_id))
        else:
            records, _ = bootstrap_ingest_source(production_id)
            total = len(records)
        job.total_files = total
        await db.commit()

        for start in range(0, total, INGEST_BATCH_SIZE):
            await run_ingest_batch(
                db, job_id, production_id, start, start + INGEST_BATCH_SIZE
            )
    except Exception as e:
        logger.exception("Inline ingest failed")
        job = await db.get(IngestJob, job_id)
        if job:
            job.status = "failed"
            job.errors = (job.errors or []) + [str(e)]
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
```

- [ ] **Step 5: Verify imports resolve**

Run (from `backend/`):
```
python -c "import app.services.ingest; import app.routers.ingest; print('ok')"
pytest -q
```
Expected: prints `ok`; test suite passes.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/ingest.py backend/app/routers/ingest.py
git commit -m "feat: add PDF batch worker and dispatch ingest by source_format"
```

---

## Task 8: Count PDFs and set source_format in /ingest/process

**Files:**
- Modify: `backend/app/routers/ingest.py` (`start_processing`, lines ~57-173)

- [ ] **Step 1: Read source_format from the request and branch the count**

In `start_processing`, after `production_id` is validated and the production
loaded, read the requested format and compute the source count accordingly.
Replace the Cloud-Tasks `try/except` that parses the source (lines ~92-108) with:

```python
    source_format = body.get("source_format", "relativity")

    if task_service.is_configured():
        # Count source items to set an accurate total_files, then enqueue tasks
        try:
            if source_format == "generic_pdf":
                from app.services.ingest_pdf import list_pdf_sources
                total_files = len(list_pdf_sources(production.id))
            else:
                records, _ = bootstrap_ingest_source(production.id)
                total_files = len(records)
        except Exception as e:
            logger.exception("Failed to parse ingest source")
            raise HTTPException(status_code=400, detail=f"Failed to parse production files: {e}")

        if total_files == 0:
            raise HTTPException(status_code=400, detail="No ingestable files found in upload")

        job = IngestJob(
            production_id=production.id,
            user_id=user.id,
            status="processing",
            source_format=source_format,
            total_files=total_files,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        enqueued = 0
        enqueue_errors: list[str] = []
        for start in range(0, total_files, INGEST_BATCH_SIZE):
            end = start + INGEST_BATCH_SIZE
```

(Leave the rest of the enqueue loop and the returned `IngestJobOut` unchanged.)

- [ ] **Step 2: Use the smaller batch size for PDFs**

PDFs can be many pages each, so use a smaller batch. At the top of
`start_processing` where `INGEST_BATCH_SIZE` is imported, also branch the batch
size. Replace the single `INGEST_BATCH_SIZE` usages in the enqueue loop with a
local `batch_size`:

Add right after `source_format = body.get("source_format", "relativity")`:

```python
    batch_size = 10 if source_format == "generic_pdf" else INGEST_BATCH_SIZE
```

Then change the enqueue loop header from
`for start in range(0, total_files, INGEST_BATCH_SIZE):` to
`for start in range(0, total_files, batch_size):` and
`end = start + INGEST_BATCH_SIZE` to `end = start + batch_size`.

- [ ] **Step 3: Set source_format on the inline fallback job too**

In the fallback branch (after the `if task_service.is_configured():` block), set
`source_format` on the `IngestJob` created there:

```python
    # Fallback: inline BackgroundTask
    total_files = body.get("total_files", 0)
    job = IngestJob(
        production_id=production.id,
        user_id=user.id,
        status="processing",
        source_format=source_format,
        total_files=total_files,
    )
```

- [ ] **Step 4: Verify imports + tests**

Run (from `backend/`):
```
python -c "import app.routers.ingest; print('ok')"
pytest -q
```
Expected: prints `ok`; tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ingest.py
git commit -m "feat: count PDFs and set source_format when starting ingest"
```

---

## Task 9: Frontend API client — forward source_format

**Files:**
- Modify: `frontend/src/api/client.ts` (lines ~328-332)

- [ ] **Step 1: Add the parameter**

Replace `startProcessing` and `reprocessProduction`:

```typescript
export const startProcessing = (
  productionId: number,
  totalFiles: number,
  sourceFormat: 'relativity' | 'generic_pdf' = 'relativity',
) =>
  request<IngestJob>('/api/ingest/process', json({ production_id: productionId, total_files: totalFiles, source_format: sourceFormat }));

export const reprocessProduction = (productionId: number) =>
  request<IngestJob>('/api/ingest/process', json({ production_id: productionId, total_files: 0 }));
```

- [ ] **Step 2: Type-check**

Run (from `frontend/`):
```
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat: forward source_format from ingest API client"
```

---

## Task 10: Frontend wizard — mode toggle, auto-detect, PDF-only upload

**Files:**
- Modify: `frontend/src/components/IngestWizard.tsx`

- [ ] **Step 1: Add mode state**

After the existing `const [files, setFiles] = useState<File[]>([]);` line, add:

```typescript
  const [mode, setMode] = useState<'relativity' | 'generic_pdf'>('relativity');
  const [modeWarning, setModeWarning] = useState('');
```

- [ ] **Step 2: Replace folder-select validation with detection + per-mode checks**

Replace the whole `handleFolderSelect` function with:

```typescript
  const handleFolderSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const fileList = e.target.files;
    if (!fileList) return;
    const selected = Array.from(fileList);

    const hasDat = selected.some(f => {
      const path = f.webkitRelativePath.toUpperCase();
      return path.includes('/DATA/') && path.endsWith('.DAT');
    });
    const pdfCount = selected.filter(f => f.name.toLowerCase().endsWith('.pdf')).length;

    // Auto-detect and pre-select the most likely mode
    const detected: 'relativity' | 'generic_pdf' = hasDat ? 'relativity' : 'generic_pdf';
    setMode(detected);

    // Warn on a mismatch between the detected mode and folder contents
    if (detected === 'relativity' && pdfCount === 0 && !hasDat) {
      setModeWarning('');
    }

    if (!hasDat && pdfCount === 0) {
      setError('Folder must contain either a DATA/*.dat file (Relativity) or at least one PDF.');
      setFiles([]);
      return;
    }

    setFiles(selected);
    setError('');
    setModeWarning('');
  };
```

- [ ] **Step 3: Recompute the warning when the user overrides the mode**

Add this handler (used by the toggle UI in Step 4):

```typescript
  const chooseMode = (next: 'relativity' | 'generic_pdf') => {
    setMode(next);
    const hasDat = files.some(f => {
      const path = f.webkitRelativePath.toUpperCase();
      return path.includes('/DATA/') && path.endsWith('.DAT');
    });
    const pdfCount = files.filter(f => f.name.toLowerCase().endsWith('.pdf')).length;
    if (next === 'relativity' && !hasDat) {
      setModeWarning('No DATA/*.dat file found in this folder — Relativity ingest will fail.');
    } else if (next === 'generic_pdf' && pdfCount === 0) {
      setModeWarning('No PDF files found in this folder.');
    } else {
      setModeWarning('');
    }
  };
```

- [ ] **Step 4: Add the toggle UI**

In the `stage === 'setup'` block, immediately after the Production Name + Description
fields and before the Production Folder field, insert the mode toggle:

```tsx
                <div>
                  <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                    Upload Type
                  </label>
                  <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                    <button
                      type="button"
                      className={mode === 'relativity' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
                      onClick={() => chooseMode('relativity')}
                    >
                      Relativity production
                    </button>
                    <button
                      type="button"
                      className={mode === 'generic_pdf' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
                      onClick={() => chooseMode('generic_pdf')}
                    >
                      Folder of files (PDFs)
                    </button>
                  </div>
                  {modeWarning && (
                    <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-warning-700, #92400e)' }}>
                      {modeWarning}
                    </div>
                  )}
                </div>
```

- [ ] **Step 5: Upload only PDFs in PDF mode, and pass the mode through**

In `handleStart`, compute the files to upload based on mode, and pass the mode to
`startProcessing`. Replace the body of `handleStart` from the
`const totalBytes = ...` line through the `startProcessing` call. Specifically:

Change the start of `handleStart`:

```typescript
  const handleStart = async () => {
    if (!name.trim() || files.length === 0) return;
    setError('');

    // In PDF mode, only upload the PDFs (skip everything else in the folder)
    const uploadList = mode === 'generic_pdf'
      ? files.filter(f => f.name.toLowerCase().endsWith('.pdf'))
      : files;

    if (uploadList.length === 0) {
      setError(mode === 'generic_pdf' ? 'No PDF files to upload.' : 'No files to upload.');
      return;
    }

    setStage('uploading');
    const totalBytes = uploadList.reduce((sum, f) => sum + f.size, 0);
    setUploadProgress({ uploaded: 0, total: uploadList.length, bytesUploaded: 0, totalBytes, startTime: Date.now() });
```

Then within `handleStart`, change the upload loop to iterate `uploadList`
instead of `files`:

```typescript
      const batchSize = 50;
      for (let i = 0; i < uploadList.length; i += batchSize) {
        await Promise.all(uploadList.slice(i, i + batchSize).map(uploadFile));
      }

      // Phase 3: Start backend processing
      setStage('processing');
      const ingestJob = await startProcessing(production_id, uploadList.length, mode);
      setJob(ingestJob);
```

(The `uploadFile` closure already uses `file.webkitRelativePath`, which is
preserved for filtered files — no change needed there.)

- [ ] **Step 6: Type-check**

Run (from `frontend/`):
```
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 7: Manual smoke test**

With backend + frontend running and Postgres up:
1. Open the ingest wizard, name a production (e.g. `PDF_TEST`).
2. Select a folder containing a few PDFs in subfolders (no DATA/).
3. Confirm the wizard auto-selects "Folder of files (PDFs)".
4. Start ingest; confirm upload completes and processing finishes.
5. Open the production; confirm documents appear titled by filename, with page
   images rendered and a "Folder" metadata value, and that full-text search
   finds words from a PDF.

Expected: documents ingested with control numbers `PDFTEST 000001`, etc.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/IngestWizard.tsx
git commit -m "feat: PDF folder upload mode in ingest wizard"
```

---

## Self-Review Notes

- **Spec coverage:** PDFs-only (Task 3/4), filename-as-title + control number (Task 4), folder-as-metadata (Task 4), 250 DPI (Task 3 `RENDER_DPI`), prefix auto-derived from project name (Task 2), explicit toggle + auto-suggest (Task 10), `source_format` column + migration (Task 5), reuse of job/Cloud-Tasks/idempotency machinery (Tasks 6-8), OCR fallback for scanned pages (Task 3), native PDF stays downloadable via `native_path` (Task 4), smaller PDF batch size of 10 (Task 8), pymupdf dependency (Task 1), tests (Tasks 2-4).
- **Idempotency:** control numbers derive from the global sorted index, so retried Cloud Tasks reproduce the same `bates_begin` and the skip-by-existing check holds (Tasks 4, 7).
- **Type/name consistency:** `render_and_extract_pdf`, `list_pdf_sources`, `process_pdf_record`, `derive_bates_prefix`, `upload_bytes`, `run_ingest_batch`, `ingest_pdf_batch`, `_incr_skipped`, `_persist_document`, `_finalize_job_if_done` are defined once and referenced consistently. `source_format` values are exactly `"relativity"` / `"generic_pdf"` across backend and frontend.
