# P2-2 Endorsement + Slip-Sheets + Production Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render a locked production set into persisted per-document endorsed PDFs (Bates + designation stamps, redaction burn-in, slip-sheets for withheld docs) via a Cloud-Tasks-batched job with dev fallback.

**Architecture:** Pure Pillow drawing in `app/services/endorse.py`; DB+storage pipeline in `app/services/production_render.py`; job fan-out mirrors ingest (`tasks.enqueue_render_batch` → OIDC-guarded worker endpoint, BackgroundTasks fallback). Render state on `production_sets` columns; per-item `output_path` doubles as progress and idempotency marker.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Pillow, google-cloud-tasks, pytest fake-session tests (no DB).

**Spec:** `docs/superpowers/specs/2026-07-22-p2-2-endorsement-rendering-design.md`

## Global Constraints

- Branch `feat/p2-2-endorsement-rendering` (stacked on `feat/p2-1-production-set-builder`; PR base = that branch until #38 merges). Verify with `git branch --show-current` before every commit.
- Migration `b8c9d0e1f2a3`, `down_revision = "a9b8c7d6e5f4"`. No `app.*` imports in the migration.
- Render statuses (exact): `not_started`, `rendering`, `rendered`, `error`.
- Disposition switch reads the P2-1 item SNAPSHOT (`item.disposition`), never recomputes.
- Rendering reads ONLY `Document.image_paths` renditions — never native/text. `redact_in_part` burns via the existing `burn_page` BEFORE stamping.
- Worker endpoints swallow render exceptions into `render_status="error"` and return 200 (Cloud Tasks would retry non-2xx forever).
- Tests: fake-session pattern, shared fakes in `backend/tests/fakes.py`; run from repo root `backend\venv\Scripts\python.exe -m pytest backend\tests\<file> -q`; 0 warnings.
- Do NOT add `Co-Authored-By`, "Generated with", or any AI-attribution trailers to commits or the PR body.

---

### Task 1: Migration + model columns

**Files:**
- Create: `backend/alembic/versions/b8c9d0e1f2a3_add_render_state.py`
- Modify: `backend/app/models.py` (`ProductionSet` and `ProductionSetItem` classes)

**Interfaces:**
- Consumes: P2-1 tables.
- Produces: `ProductionSet.render_status/render_error/rendered_at`; `ProductionSetItem.output_path` (Tasks 3-4 read/write these).

- [ ] **Step 1: Write the migration**

Create `backend/alembic/versions/b8c9d0e1f2a3_add_render_state.py`:

```python
"""add production-set render state

Revision ID: b8c9d0e1f2a3
Revises: a9b8c7d6e5f4
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "b8c9d0e1f2a3"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("production_sets", sa.Column("render_status", sa.String(length=20), nullable=False, server_default=sa.text("'not_started'")))
    op.add_column("production_sets", sa.Column("render_error", sa.Text(), nullable=True))
    op.add_column("production_sets", sa.Column("rendered_at", sa.DateTime(), nullable=True))
    op.add_column("production_set_items", sa.Column("output_path", sa.String(length=500), nullable=True))


def downgrade():
    op.drop_column("production_set_items", "output_path")
    op.drop_column("production_sets", "rendered_at")
    op.drop_column("production_sets", "render_error")
    op.drop_column("production_sets", "render_status")
```

- [ ] **Step 2: Update models**

In `backend/app/models.py`, in `ProductionSet` after `locked_at`:

```python
    # P2-2 — render state
    render_status = Column(String(20), nullable=False, default="not_started")  # not_started|rendering|rendered|error
    render_error = Column(Text, nullable=True)
    rendered_at = Column(DateTime, nullable=True)
```

In `ProductionSetItem` after `designation`:

```python
    output_path = Column(String(500), nullable=True)  # GCS path of rendered PDF
```

- [ ] **Step 3: Verify compile, purity, single head, models import**

```
backend\venv\Scripts\python.exe -m py_compile backend\alembic\versions\b8c9d0e1f2a3_add_render_state.py
cd backend && venv\Scripts\python.exe -c "import app.models"
```
Grep `a9b8c7d6e5f4` in `backend/alembic/versions` — exactly two hits (its own `revision` line and this file's `down_revision`). Migration contains no `app.` imports.

- [ ] **Step 4: Commit**

```bash
git add backend/alembic/versions/b8c9d0e1f2a3_add_render_state.py backend/app/models.py
git commit -m "feat(p2-2): render-state columns on production sets + items"
```

---

### Task 2: Pure endorsement service

**Files:**
- Create: `backend/app/services/endorse.py`
- Test: `backend/tests/test_endorse.py`

**Interfaces:**
- Consumes: `format_bates` from `app.services.production_numbering`.
- Produces (Task 3 imports): `SLIP_W, SLIP_H`, `page_bates_numbers(bates_begin, prefix, padding, page_count) -> list[str]`, `stamp_page(img, bates_text, designation) -> Image`, `slip_sheet(bates_text, designation, title="DOCUMENT WITHHELD") -> Image`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_endorse.py`:

```python
"""Pure Pillow tests for endorsement stamping (P2-2). No DB/network."""

from PIL import Image

from app.services.endorse import (
    SLIP_H,
    SLIP_W,
    page_bates_numbers,
    slip_sheet,
    stamp_page,
)

GRAY = (60, 60, 60)


def _white_count(img, box):
    """Count pure-white pixels inside box=(l, t, r, b)."""
    region = img.crop(box)
    return sum(1 for px in region.getdata() if px == (255, 255, 255))


# --- page_bates_numbers -----------------------------------------------------

def test_page_bates_sequence():
    out = page_bates_numbers("SMITH000005", "SMITH", 6, 3)
    assert out == ["SMITH000005", "SMITH000006", "SMITH000007"]


def test_page_bates_single_page():
    assert page_bates_numbers("VOL0100", "VOL", 4, 1) == ["VOL0100"]


def test_page_bates_overflow_grows():
    out = page_bates_numbers("SMITH999999", "SMITH", 6, 2)
    assert out == ["SMITH999999", "SMITH1000000"]


# --- stamp_page -------------------------------------------------------------

def test_stamp_page_returns_copy_and_stamps_bottom_right():
    img = Image.new("RGB", (400, 600), GRAY)
    out = stamp_page(img, "SMITH000001", None)
    assert out is not img
    assert img.getpixel((390, 590)) == GRAY  # original untouched
    h = out.height
    # white backing box appears in the bottom-right strip
    assert _white_count(out, (200, h - 80, 400, h)) > 0
    # nothing stamped bottom-left when designation is None
    assert _white_count(out, (0, h - 80, 200, h)) == 0
    # top of page untouched
    assert _white_count(out, (0, 0, 400, 100)) == 0


def test_stamp_page_designation_bottom_left():
    img = Image.new("RGB", (400, 600), GRAY)
    out = stamp_page(img, "SMITH000001", "CONFIDENTIAL")
    h = out.height
    assert _white_count(out, (0, h - 80, 200, h)) > 0


def test_stamp_page_converts_to_rgb():
    img = Image.new("L", (400, 600), 60)
    out = stamp_page(img, "SMITH000001", None)
    assert out.mode == "RGB"


# --- slip_sheet -------------------------------------------------------------

def test_slip_sheet_dimensions_and_content():
    page = slip_sheet("SMITH000001", "CONFIDENTIAL")
    assert (page.width, page.height) == (SLIP_W, SLIP_H)
    assert page.getpixel((5, 5)) == (255, 255, 255)  # white page
    # title text renders as non-white pixels in the middle band
    mid = page.crop((0, SLIP_H // 2 - 100, SLIP_W, SLIP_H // 2 + 100))
    assert any(px != (255, 255, 255) for px in mid.getdata())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_endorse.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.endorse'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/endorse.py`:

```python
"""Pure endorsement drawing for produced documents (P2-2). No DB/network.

Stamps are black text on a white backing box so they stay legible on dark
scans. Fonts follow the codebase pattern: DejaVu with load_default fallback.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from app.services.production_numbering import format_bates

SLIP_W, SLIP_H = 1240, 1754  # A4 @ ~150 DPI, matches documents.py index pages
_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _load_font(size: int):
    try:
        return ImageFont.truetype(_DEJAVU_BOLD, size)
    except Exception:
        return ImageFont.load_default(size)


def page_bates_numbers(
    bates_begin: str, prefix: str, padding: int, page_count: int
) -> list[str]:
    """Every produced page carries its own sequential Bates number."""
    start = int(bates_begin[len(prefix):])
    return [format_bates(prefix, start + i, padding) for i in range(page_count)]


def _stamp_text(draw: ImageDraw.ImageDraw, img_w: int, img_h: int,
                text: str, font, corner: str) -> None:
    """corner: 'br' (Bates) or 'bl' (designation)."""
    pad = 8
    margin = max(10, int(img_h * 0.015))
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if corner == "br":
        x = img_w - margin - tw - pad * 2
    else:
        x = margin
    y = img_h - margin - th - pad * 2
    draw.rectangle([x, y, x + tw + pad * 2, y + th + pad * 2],
                   fill="white", outline="black")
    draw.text((x + pad - bbox[0], y + pad - bbox[1]), text, fill="black", font=font)


def stamp_page(img: Image.Image, bates_text: str,
               designation: str | None) -> Image.Image:
    """Return a stamped RGB copy: Bates bottom-right, designation bottom-left."""
    out = img.convert("RGB") if img.mode != "RGB" else img.copy()
    draw = ImageDraw.Draw(out)
    font = _load_font(max(14, out.height // 60))
    _stamp_text(draw, out.width, out.height, bates_text, font, "br")
    if designation:
        _stamp_text(draw, out.width, out.height, designation, font, "bl")
    return out


def slip_sheet(bates_text: str, designation: str | None,
               title: str = "DOCUMENT WITHHELD") -> Image.Image:
    """One white A4 page standing in for a withheld document."""
    page = Image.new("RGB", (SLIP_W, SLIP_H), "white")
    draw = ImageDraw.Draw(page)
    font = _load_font(48)
    bbox = draw.textbbox((0, 0), title, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((SLIP_W - tw) / 2 - bbox[0], (SLIP_H - th) / 2 - bbox[1]),
              title, fill="black", font=font)
    return stamp_page(page, bates_text, designation)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_endorse.py -q`
Expected: 7 passed, 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/endorse.py backend/tests/test_endorse.py
git commit -m "feat(p2-2): pure endorsement service (per-page Bates, stamps, slip-sheets)"
```

---

### Task 3: Render pipeline service

**Files:**
- Create: `backend/app/services/production_render.py`
- Test: `backend/tests/test_production_render.py`

**Interfaces:**
- Consumes: models (Task 1 columns), `endorse` (Task 2), `burn_page` (`app.services.redaction_render`), `storage` module.
- Produces (Task 4 imports from `app.services.production_render`): `artifact_path(production_id, set_id, bates_begin) -> str`, `render_item(db, ps, item) -> str`, `render_batch(db, set_id, document_ids) -> int`, `finalize_if_complete(db, set_id) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_production_render.py`:

```python
"""Fake-session tests for the production render pipeline (P2-2). No DB/GCS."""

import asyncio
from uuid import uuid4

import pytest
from PIL import Image

import app.services.production_render as pr
from tests.fakes import TS, FakeResult, FakeSession

GRAY = (60, 60, 60)


class FakePS:
    def __init__(self, set_id=1, production_id=1, **kw):
        self.id = set_id
        self.production_id = production_id
        self.status = kw.get("status", "locked")
        self.prefix = kw.get("prefix", "SMITH")
        self.padding = kw.get("padding", 6)
        self.designation = kw.get("designation", None)
        self.render_status = kw.get("render_status", "rendering")
        self.render_error = None
        self.rendered_at = None


class FakeItem:
    def __init__(self, document_id, disposition="produce", **kw):
        self.id = kw.get("item_id", 1)
        self.document_id = document_id
        self.disposition = disposition
        self.bates_begin = kw.get("bates_begin", "SMITH000001")
        self.bates_end = kw.get("bates_end", "SMITH000002")
        self.pages = kw.get("pages", 2)
        self.designation = kw.get("designation", None)
        self.output_path = kw.get("output_path", None)


class FakeDoc:
    def __init__(self, doc_id, image_paths=("p1.jpg", "p2.jpg")):
        self.id = doc_id
        self.bates_begin = "C-1"
        self.image_paths = list(image_paths)


class FakeRed:
    def __init__(self, page_num):
        self.page_num = page_num
        self.x_pct, self.y_pct, self.w_pct, self.h_pct = 10.0, 10.0, 20.0, 20.0
        self.reason_code = "pii"


def _spies(monkeypatch):
    uploads, burns = [], []

    def fake_upload(data, path, content_type=None):
        uploads.append((path, content_type, len(data)))
        return path

    def fake_load(raw_path):
        return Image.new("RGB", (200, 300), GRAY)

    def fake_burn(img, rects):
        burns.append(len(rects))
        return img

    monkeypatch.setattr(pr.storage, "upload_bytes", fake_upload)
    monkeypatch.setattr(pr, "_load_page", fake_load)
    monkeypatch.setattr(pr, "burn_page", fake_burn)
    return uploads, burns


def test_artifact_path_layout():
    assert pr.artifact_path(3, 7, "SMITH000001") == \
        "productions/3/production_sets/7/SMITH000001.pdf"


def test_render_item_withhold_uploads_single_slipsheet(monkeypatch):
    uploads, burns = _spies(monkeypatch)
    doc_id = uuid4()
    item = FakeItem(doc_id, disposition="withhold", pages=1)
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    path = asyncio.run(pr.render_item(db, FakePS(), item))
    assert path == "productions/1/production_sets/1/SMITH000001.pdf"
    assert item.output_path == path
    assert uploads == [(path, "application/pdf", uploads[0][2])]
    assert burns == []  # withheld docs never touch page images


def test_render_item_redact_in_part_burns_then_stamps(monkeypatch):
    uploads, burns = _spies(monkeypatch)
    doc_id = uuid4()
    item = FakeItem(doc_id, disposition="redact_in_part")
    db = FakeSession(
        get_objects={("Document", doc_id): FakeDoc(doc_id)},
        responders=[("FROM redactions", FakeResult(items=[FakeRed(1)]))],
    )
    asyncio.run(pr.render_item(db, FakePS(), item))
    assert burns == [1]  # page 1 only; page 2 has no redactions
    assert len(uploads) == 1


def test_render_item_produce_never_burns(monkeypatch):
    uploads, burns = _spies(monkeypatch)
    doc_id = uuid4()
    item = FakeItem(doc_id, disposition="produce")
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    asyncio.run(pr.render_item(db, FakePS(), item))
    assert burns == []
    assert len(uploads) == 1


def test_render_item_zero_readable_pages_raises(monkeypatch):
    _spies(monkeypatch)
    monkeypatch.setattr(pr, "_load_page", lambda raw_path: None)
    doc_id = uuid4()
    item = FakeItem(doc_id, disposition="produce")
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    with pytest.raises(RuntimeError):
        asyncio.run(pr.render_item(db, FakePS(), item))


def test_render_batch_skips_done_and_marks_error(monkeypatch):
    _spies(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    done = FakeItem(d1, output_path="already/there.pdf")
    pending = FakeItem(d2)
    ps = FakePS()

    async def boom(db, ps_, item):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(pr, "render_item", boom)
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("FROM production_set_items", FakeResult(items=[done, pending]))],
    )
    n = asyncio.run(pr.render_batch(db, 1, [d1, d2]))
    assert n == 0
    assert ps.render_status == "error"
    assert "render exploded" in ps.render_error


def test_finalize_flips_status_only_when_all_rendered():
    ps = FakePS(render_status="rendering")
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("count", FakeResult(scalar=0))],
    )
    assert asyncio.run(pr.finalize_if_complete(db, 1)) is True
    assert ps.render_status == "rendered"
    assert ps.rendered_at is not None

    ps2 = FakePS(render_status="rendering")
    db2 = FakeSession(
        get_objects={("ProductionSet", 1): ps2},
        responders=[("count", FakeResult(scalar=3))],
    )
    assert asyncio.run(pr.finalize_if_complete(db2, 1)) is False
    assert ps2.render_status == "rendering"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_render.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.production_render'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/production_render.py`:

```python
"""Render locked production sets into endorsed PDFs (P2-2). DB + storage.

Reads ONLY image_paths renditions (never native/text); redact_in_part pages
are burned via burn_page BEFORE stamping, so redacted pixels cannot reach a
produced PDF. Pure drawing lives in endorse.py.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, ProductionSet, ProductionSetItem, Redaction
from app.services import storage
from app.services.endorse import page_bates_numbers, slip_sheet, stamp_page
from app.services.redaction_render import burn_page

logger = logging.getLogger(__name__)


def artifact_path(production_id: int, set_id: int, bates_begin: str) -> str:
    return f"productions/{production_id}/production_sets/{set_id}/{bates_begin}.pdf"


def _load_page(raw_path: str) -> Image.Image | None:
    """Same selection rule as documents.py: productions/ prefix -> GCS."""
    try:
        if raw_path.startswith("productions/"):
            data = storage.get_download_bytes(raw_path)
            return Image.open(io.BytesIO(data)).convert("RGB")
        p = Path(raw_path.replace("\\", "/")).resolve()
        return Image.open(p).convert("RGB")
    except Exception:
        logger.warning("Unreadable page image: %s", raw_path)
        return None


async def render_item(db: AsyncSession, ps: ProductionSet,
                      item: ProductionSetItem) -> str:
    doc = await db.get(Document, item.document_id)
    designation = item.designation or ps.designation

    if item.disposition == "withhold":
        pages = [slip_sheet(item.bates_begin, designation)]
    else:
        reds_by_page: dict[int, list] = {}
        if item.disposition == "redact_in_part":
            reds = (await db.execute(
                select(Redaction).where(Redaction.document_id == item.document_id)
            )).scalars().all()
            for r in reds:
                reds_by_page.setdefault(r.page_num, []).append(r)
        bates = page_bates_numbers(item.bates_begin, ps.prefix, ps.padding,
                                   item.pages or 1)
        pages = []
        for idx, raw_path in enumerate(doc.image_paths or [], start=1):
            img = _load_page(raw_path)
            if img is None:
                continue
            if reds_by_page.get(idx):
                img = burn_page(img, reds_by_page[idx])
            # guard drift between lock snapshot and current image count
            bates_text = bates[min(idx, len(bates)) - 1]
            pages.append(stamp_page(img, bates_text, designation))
        if not pages:
            raise RuntimeError(f"No readable page images for {doc.bates_begin}")

    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True,
                  append_images=pages[1:], resolution=150.0)
    path = artifact_path(ps.production_id, ps.id, item.bates_begin)
    storage.upload_bytes(buf.getvalue(), path, "application/pdf")
    item.output_path = path
    return path


async def render_batch(db: AsyncSession, set_id: int, document_ids: list) -> int:
    """Worker unit. Commits after each item; marks the set errored on failure
    and returns instead of raising (Cloud Tasks would retry non-2xx forever)."""
    ps = await db.get(ProductionSet, set_id)
    if not ps or ps.status != "locked":
        return 0
    items = (await db.execute(
        select(ProductionSetItem).where(
            ProductionSetItem.production_set_id == set_id,
            ProductionSetItem.document_id.in_(document_ids),
        )
    )).scalars().all()
    rendered = 0
    for item in items:
        if item.output_path:
            continue  # idempotent retry / resume
        try:
            await render_item(db, ps, item)
            await db.commit()
            rendered += 1
        except Exception as exc:
            logger.exception("Render failed for set %s doc %s", set_id, item.document_id)
            ps.render_status = "error"
            ps.render_error = str(exc)
            await db.commit()
            return rendered
    return rendered


async def finalize_if_complete(db: AsyncSession, set_id: int) -> bool:
    ps = await db.get(ProductionSet, set_id)
    if not ps or ps.render_status != "rendering":
        return False
    remaining = (await db.execute(
        select(func.count(ProductionSetItem.id)).where(
            ProductionSetItem.production_set_id == set_id,
            ProductionSetItem.output_path.is_(None),
        )
    )).scalar() or 0
    if remaining:
        return False
    ps.render_status = "rendered"
    ps.render_error = None
    ps.rendered_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_render.py backend\tests\test_endorse.py -q`
Expected: 14 passed (7 + 7), 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/production_render.py backend/tests/test_production_render.py
git commit -m "feat(p2-2): render pipeline — slip-sheets, burn-then-stamp, per-item resume"
```

---

### Task 4: Task fan-out + endpoints + schema fields

**Files:**
- Modify: `backend/app/services/tasks.py` (append `enqueue_render_batch`)
- Modify: `backend/app/schemas.py` (`ProductionSetOut` gains render fields)
- Modify: `backend/app/routers/production_sets.py` (imports + three endpoints + detail update)
- Test: `backend/tests/test_production_set_endpoints.py` (append; extend `FakePS`)

**Interfaces:**
- Consumes: Task 3 pipeline, `verify_cloud_tasks_request` (`app.services.oidc`), `tasks.is_configured`, `get_signed_url` (`app.services.storage`), `async_session` (`app.database`).
- Produces: `POST /api/production-sets/{set_id}/render` → `{documents, batches}`; `POST /api/production-sets/render-batch` (OIDC worker) → `{rendered}`; `GET /api/production-sets/{set_id}/documents/{document_id}/pdf` → 307 redirect to signed URL. `ProductionSetOut` fields `render_status`, `render_error`, `rendered_at`, `rendered_count`.

- [ ] **Step 1: Extend FakePS + write the failing tests**

In `backend/tests/test_production_set_endpoints.py`, add to `FakePS.__init__` (after `self.locked_at = None`):

```python
        self.render_status = kw.get("render_status", "not_started")
        self.render_error = None
        self.rendered_at = None
```

Append to the file:

```python
# --- render endpoints (P2-2) ------------------------------------------------

class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def test_render_trigger_requires_locked(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="draft")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.render_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_render_trigger_409_while_running(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked", render_status="rendering")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.render_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_render_trigger_batches_and_fallback(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(rps.tasks, "is_configured", lambda: False)
    ps = FakePS(status="locked")
    doc_ids = [(uuid4(),) for _ in range(30)]
    bg = FakeBackgroundTasks()
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("production_set_items", FakeResult(rows=doc_ids))],
    )
    out = asyncio.run(rps.render_production_set(
        set_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert out == {"documents": 30, "batches": 2}  # 25 + 5
    assert ps.render_status == "rendering"
    assert len(bg.tasks) == 1  # one inline runner covering all batches


def test_render_trigger_empty_set_422(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.render_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_render_worker_calls_pipeline(monkeypatch):
    calls = {}

    async def fake_render_batch(db, set_id, document_ids):
        calls["batch"] = (set_id, len(document_ids))
        return len(document_ids)

    async def fake_finalize(db, set_id):
        calls["finalized"] = set_id
        return True

    monkeypatch.setattr(rps, "render_batch", fake_render_batch)
    monkeypatch.setattr(rps, "finalize_if_complete", fake_finalize)
    d1, d2 = uuid4(), uuid4()
    out = asyncio.run(rps.render_batch_handler(
        body={"set_id": 1, "document_ids": [str(d1), str(d2)]},
        db=FakeSession(), _verified=None))
    assert out == {"rendered": 2}
    assert calls == {"batch": (1, 2), "finalized": 1}


def test_produced_pdf_404_before_render(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS(status="locked")},
        responders=[("production_set_items", FakeResult(items=[FakeItem(d1)]))],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.get_produced_pdf(set_id=1, document_id=d1, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_produced_pdf_redirects_to_signed_url(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(rps, "get_signed_url",
                        lambda path, **kw: f"https://signed.example/{path}")
    d1 = uuid4()
    item = FakeItem(d1, bates_begin="SMITH000004")
    item.output_path = "productions/1/production_sets/1/SMITH000004.pdf"
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS(status="locked")},
        responders=[("production_set_items", FakeResult(items=[item]))],
    )
    out = asyncio.run(rps.get_produced_pdf(set_id=1, document_id=d1, db=db, user=FakeUser()))
    assert out.status_code == 307
    assert out.headers["location"].endswith("SMITH000004.pdf")


def test_detail_includes_render_progress(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    ps = FakePS(status="locked", render_status="rendering")
    i1 = FakeItem(d1, item_id=1, sort_order=1, bates_begin="SMITH000001",
                  bates_end="SMITH000001", pages=1)
    i1.output_path = "productions/1/production_sets/1/SMITH000001.pdf"
    i2 = FakeItem(d2, item_id=2, sort_order=2, bates_begin="SMITH000002",
                  bates_end="SMITH000002", pages=1)
    i2.output_path = None
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("FROM production_set_items", FakeResult(items=[i1, i2]))],
    )
    out = asyncio.run(rps.get_production_set(set_id=1, db=db, user=FakeUser()))
    assert out.render_status == "rendering"
    assert out.rendered_count == 1
```

Note: `FakeItem` predates P2-2, so `output_path` is set as a plain attribute in tests; also add `self.output_path = kw.get("output_path", None)` to `FakeItem.__init__` for cleanliness.

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_set_endpoints.py -q -k "render or produced or progress"`
Expected: FAIL with `AttributeError: module 'app.routers.production_sets' has no attribute 'render_production_set'`

- [ ] **Step 3: Implement**

(a) `backend/app/services/tasks.py` — append:

```python
def enqueue_render_batch(set_id: int, document_ids: list[str]) -> None:
    """Enqueue a Cloud Task to render one batch of production-set documents."""
    if not is_configured():
        raise RuntimeError("Cloud Tasks not configured")

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    handler_url = f"{settings.cloud_run_service_url}/api/production-sets/render-batch"
    payload = json.dumps({"set_id": set_id, "document_ids": document_ids}).encode()

    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=handler_url,
            headers={"Content-Type": "application/json"},
            body=payload,
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.cloud_tasks_service_account,
                audience=settings.cloud_run_service_url,
            ),
        ),
        # Rendering a batch (image download + burn + stamp + PDF + upload)
        # can run long; use the Cloud Tasks maximum dispatch deadline.
        dispatch_deadline=duration_pb2.Duration(seconds=1800),
    )

    client.create_task(parent=queue_path, task=task)
    logger.info("Enqueued render batch for set %d: %d documents",
                set_id, len(document_ids))
```

(b) `backend/app/schemas.py` — in `ProductionSetOut`, after `bates_end`:

```python
    render_status: str = "not_started"
    render_error: str | None = None
    rendered_at: datetime | None = None
    rendered_count: int = 0
```

(c) `backend/app/routers/production_sets.py`:
- imports: add `BackgroundTasks` to the fastapi import; add `update as sa_update` to the sqlalchemy import (beside `delete as sa_delete`); add `from fastapi.responses import RedirectResponse`; add `from app.services import tasks`; add `from app.services.oidc import verify_cloud_tasks_request`; add `from app.services.storage import get_signed_url`; add `from app.services.production_render import finalize_if_complete, render_batch`.
- In `get_production_set`, after `out.doc_count = len(items)` add:

```python
    out.rendered_count = sum(1 for i in items if i.output_path)
```

- Append endpoints:

```python
RENDER_BATCH_SIZE = 25


@router.post("/production-sets/{set_id}/render")
async def render_production_set(
    set_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "locked":
        raise HTTPException(status_code=409, detail="Production set must be locked before rendering")
    if ps.render_status == "rendering":
        raise HTTPException(status_code=409, detail="Render already in progress")

    rows = (await db.execute(
        select(ProductionSetItem.document_id)
        .where(ProductionSetItem.production_set_id == set_id)
        .order_by(ProductionSetItem.sort_order)
    )).all()
    doc_ids = [r[0] for r in rows]
    if not doc_ids:
        raise HTTPException(status_code=422, detail="Production set has no members")

    # Re-render semantics: clear prior artifact markers, then rebuild all.
    await db.execute(
        sa_update(ProductionSetItem)
        .where(ProductionSetItem.production_set_id == set_id)
        .values(output_path=None)
    )
    ps.render_status = "rendering"
    ps.render_error = None
    ps.rendered_at = None
    batches = [doc_ids[i:i + RENDER_BATCH_SIZE]
               for i in range(0, len(doc_ids), RENDER_BATCH_SIZE)]
    await log_action(db, user, "production_set_render_started", "production_set",
                     str(set_id), production_id=ps.production_id,
                     details={"documents": len(doc_ids), "batches": len(batches)})
    await db.commit()

    if tasks.is_configured():
        for batch in batches:
            tasks.enqueue_render_batch(set_id, [str(d) for d in batch])
    else:
        background_tasks.add_task(_render_inline, set_id, batches)
    return {"documents": len(doc_ids), "batches": len(batches)}


async def _render_inline(set_id: int, batches):
    """Dev fallback: run all batches in-process on a fresh session."""
    from app.database import async_session

    async with async_session() as db:
        for batch in batches:
            await render_batch(db, set_id, batch)
        await finalize_if_complete(db, set_id)


@router.post("/production-sets/render-batch")
async def render_batch_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker endpoint — renders one batch of set documents.

    Always returns 200; render failures land in render_status='error'
    (Cloud Tasks retries non-2xx, which would loop a deterministic failure).
    """
    set_id = body.get("set_id")
    document_ids = body.get("document_ids")
    if set_id is None or not document_ids:
        raise HTTPException(status_code=400, detail="Missing required fields")
    n = await render_batch(db, int(set_id), [UUID(d) for d in document_ids])
    await finalize_if_complete(db, int(set_id))
    return {"rendered": n}


@router.get("/production-sets/{set_id}/documents/{document_id}/pdf")
async def get_produced_pdf(
    set_id: int,
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _load_set(db, user, set_id)
    items = (await db.execute(
        select(ProductionSetItem).where(
            ProductionSetItem.production_set_id == set_id,
            ProductionSetItem.document_id == document_id,
        )
    )).scalars().all()
    if not items or not items[0].output_path:
        raise HTTPException(status_code=404, detail="Rendered output not found")
    item = items[0]
    url = get_signed_url(
        item.output_path,
        response_disposition=f'attachment; filename="{item.bates_begin}.pdf"',
    )
    return RedirectResponse(url, status_code=307)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_set_endpoints.py backend\tests\test_production_render.py backend\tests\test_endorse.py -q`
Expected: all pass (38 endpoint + 7 render + 7 endorse), 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/tasks.py backend/app/schemas.py backend/app/routers/production_sets.py backend/tests/test_production_set_endpoints.py
git commit -m "feat(p2-2): render trigger + Cloud Tasks worker + produced-PDF endpoint"
```

---

### Task 5: Full-suite verification + PR

**Files:** none new.

**Interfaces:** n/a — verification gate.

- [ ] **Step 1: Full suite**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests -q`
Expected: everything passes except the known pre-existing `test_ai_review.py::test_build_classification_prompt`. Any other failure = regression; fix code, never old tests.

- [ ] **Step 2: Migration head + purity re-check**

Grep `backend/alembic/versions` for `down_revision` containing `a9b8c7d6e5f4` — exactly one file (`b8c9d0e1f2a3...`). Confirm no `app.` imports in it.

- [ ] **Step 3: Push and open PR (stacked on P2-1)**

```bash
git push -u origin feat/p2-2-endorsement-rendering
gh pr create --base feat/p2-1-production-set-builder --title "feat(p2-2): endorsement, slip-sheets, production rendering" --body "$(cat <<'EOF'
## Summary
- Pure endorsement service: per-page Bates numbers, corner stamps (Bates bottom-right, confidentiality designation bottom-left, white backing box), A4 slip-sheets for withheld docs
- Render pipeline: disposition-driven (withhold -> slip-sheet; redact_in_part -> burn redactions THEN stamp; produce -> stamp), assembles per-doc PDFs, persists to GCS under productions/{id}/production_sets/{set_id}/
- Job orchestration mirrors ingest: Cloud Tasks batches (OIDC-guarded worker) with BackgroundTasks dev fallback; per-item output_path = progress + idempotent resume; render_status state machine on the set
- Signed-URL endpoint for spot-checking rendered output

Stacked on #38 (P2-1). Spec: docs/superpowers/specs/2026-07-22-p2-2-endorsement-rendering-design.md

## Test plan
- [x] Pure Pillow tests: stamp placement/copy semantics, slip-sheet dimensions, per-page Bates sequences incl. padding overflow
- [x] Pipeline tests: disposition switch, burn-only-redacted-pages, zero-readable-pages failure, batch resume + error capture, finalize gating
- [x] Endpoint tests: locked/running 409s, batch math + fallback, worker delegation, 404-before-render, 307 signed-URL redirect, detail progress
- [x] Full backend suite green (1 pre-existing unrelated failure)
EOF
)"
```
