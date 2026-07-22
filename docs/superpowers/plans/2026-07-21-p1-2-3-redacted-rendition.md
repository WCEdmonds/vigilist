# P1-2/3 Redacted Rendition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opt-in `?redacted=1` rendition on the image/pdf/text document endpoints that burns redaction boxes into page images, drops annotations, and withholds extracted text — plus a pure burn-in service Phase 2 will reuse.

**Architecture:** A new pure PIL module `app/services/redaction_render.py` (`burn_page`) does all pixel work. The three endpoints in `app/routers/documents.py` gain a `redacted: bool` query param that fetches the doc's `Redaction` rows and applies `burn_page` / withholds text. `DocumentDetail` gains a computed `redaction_count`. No migration.

**Tech Stack:** FastAPI, SQLAlchemy async, Pillow ≥ 11, pytest (fake-session unit tests, no DB — same pattern as `tests/test_redaction_endpoints.py`).

**Spec:** `docs/superpowers/specs/2026-07-21-p1-2-3-redacted-rendition-design.md`

## Global Constraints

- With `redacted` absent/false, every endpoint's behavior must be exactly today's (flag-off = byte-identical paths).
- `burn_page` is pure: no DB, no network, no storage, input image never mutated.
- `?redacted=1` + zero redactions ⇒ normal rendition (image/pdf identical to flag-off; text returns normal text with `"withheld": false`).
- Redacted PDF omits annotation pins AND the annotation index pages.
- No new dependencies; no schema migration. Modules importable without full app deps stay that way (CI runs alembic under minimal deps — but nothing here is imported by migrations).
- Run tests from repo root with: `backend\venv\Scripts\python.exe -m pytest backend\tests\<file> -q`
- Access control unchanged: same production-access checks, no role gating on the flag.
- Commit after every green test cycle; work on branch `feat/p1-2-3-redacted-rendition`.

---

### Task 1: `burn_page` pure service

**Files:**
- Create: `backend/app/services/redaction_render.py`
- Test: `backend/tests/test_redaction_render.py`

**Interfaces:**
- Consumes: nothing (pure PIL).
- Produces: `burn_page(img: PIL.Image.Image, rects: Sequence[RectLike]) -> PIL.Image.Image` where `RectLike` has attributes `x_pct, y_pct, w_pct, h_pct` (floats, 0–100 normalized) and `reason_code: str`. Returns a new RGB image; never mutates the input. Tasks 2–3 import it as `from app.services.redaction_render import burn_page`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_redaction_render.py`:

```python
"""Pixel-level tests for the pure redaction burn-in service (P1-2/3)."""

from dataclasses import dataclass

from PIL import Image

from app.services.redaction_render import burn_page


@dataclass
class Rect:
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    reason_code: str = "pii"


def _white(w=1000, h=1000):
    return Image.new("RGB", (w, h), "white")


def test_pixels_inside_rect_are_black_outside_untouched():
    img = _white()
    out = burn_page(img, [Rect(10, 10, 20, 20)])
    # dead center of the box (20%, 20%) -> (200, 200)
    assert out.getpixel((200, 200)) == (0, 0, 0)
    # well outside the box
    assert out.getpixel((500, 500)) == (255, 255, 255)
    assert out.getpixel((50, 50)) == (255, 255, 255)


def test_input_image_not_mutated():
    img = _white()
    burn_page(img, [Rect(0, 0, 50, 50)])
    assert img.getpixel((100, 100)) == (255, 255, 255)


def test_multiple_and_overlapping_rects():
    img = _white()
    out = burn_page(img, [Rect(0, 0, 30, 30), Rect(20, 20, 30, 30), Rect(60, 60, 10, 10)])
    assert out.getpixel((100, 100)) == (0, 0, 0)    # first rect
    assert out.getpixel((250, 250)) == (0, 0, 0)    # overlap zone
    assert out.getpixel((650, 650)) == (0, 0, 0)    # third rect
    assert out.getpixel((990, 990)) == (255, 255, 255)


def test_edge_hugging_rect_stays_in_bounds():
    img = _white()
    out = burn_page(img, [Rect(80, 90, 20, 10)])  # x+w = 100, y+h = 100
    assert out.size == (1000, 1000)
    assert out.getpixel((999, 999)) == (0, 0, 0)
    assert out.getpixel((799, 899)) == (255, 255, 255)


def test_label_renders_in_large_box():
    img = _white()
    out = burn_page(img, [Rect(10, 10, 60, 20, reason_code="attorney_client")])
    # label is white text inside the black box -> some non-black pixels inside
    box = out.crop((100, 100, 700, 300))
    colors = box.getcolors(maxcolors=1_000_000)
    non_black = [c for c in colors if c[1] != (0, 0, 0)]
    assert non_black, "expected white label pixels inside large box"


def test_tiny_box_is_solid_black_no_label():
    img = _white()
    out = burn_page(img, [Rect(10, 10, 2, 1, reason_code="attorney_client")])
    box = out.crop((100, 100, 120, 110))
    colors = box.getcolors(maxcolors=1_000_000)
    assert colors is not None and len(colors) == 1 and colors[0][1] == (0, 0, 0)


def test_empty_rects_returns_equal_image():
    img = _white(200, 100)
    out = burn_page(img, [])
    assert out is not img
    assert list(out.getdata()) == list(img.getdata())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redaction_render.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.redaction_render'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/redaction_render.py`:

```python
"""Burn redaction rectangles into page images (P1-2/3).

Pure pixel work: no DB, no network, no storage. The as-produced rendition
endpoints and (later) the Phase-2 production pipeline both call burn_page.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from PIL import Image, ImageDraw, ImageFont


class RectLike(Protocol):
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    reason_code: str


# White stamp shown inside the black box when it fits. Privilege logs
# cross-reference these labels, so keep them stable.
REASON_LABELS: dict[str, str] = {
    "attorney_client": "ATTORNEY-CLIENT",
    "work_product": "WORK PRODUCT",
    "pii": "PII",
    "phi": "PHI",
    "confidential": "CONFIDENTIAL",
    "trade_secret": "TRADE SECRET",
    "non_responsive": "NON-RESPONSIVE",
    "other": "REDACTED",
}

_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(_DEJAVU_BOLD, size)
    except Exception:
        return ImageFont.load_default(size)  # Pillow >= 10.1 scalable fallback


def burn_page(img: Image.Image, rects: Sequence[RectLike]) -> Image.Image:
    """Return a copy of img with each rect burned in as an opaque black box.

    Coordinates are 0-100 percentages of the page. The reason-code label is
    stamped in white, centered, only when it fits inside the box.
    """
    out = img.copy()
    if not rects:
        return out
    if out.mode != "RGB":
        out = out.convert("RGB")
    draw = ImageDraw.Draw(out)
    for r in rects:
        x0 = r.x_pct / 100.0 * out.width
        y0 = r.y_pct / 100.0 * out.height
        x1 = min(out.width, (r.x_pct + r.w_pct) / 100.0 * out.width)
        y1 = min(out.height, (r.y_pct + r.h_pct) / 100.0 * out.height)
        draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0))

        label = REASON_LABELS.get(r.reason_code, "REDACTED")
        box_w, box_h = x1 - x0, y1 - y0
        font = _load_font(max(12, int(box_h * 0.35)))
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= box_w * 0.9 and th <= box_h * 0.8:
            # Center manually (anchor support varies by font backend).
            pos = (x0 + (box_w - tw) / 2 - bbox[0], y0 + (box_h - th) / 2 - bbox[1])
            draw.text(pos, label, fill=(255, 255, 255), font=font)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redaction_render.py -q`
Expected: 7 passed. If `test_tiny_box_is_solid_black_no_label` fails because the label fit anyway, the fit thresholds (0.9/0.8) are wrong — shrink them, don't change the test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/redaction_render.py backend/tests/test_redaction_render.py
git commit -m "feat(p1-2/3): pure burn_page redaction render service"
```

---

### Task 2: Shared endpoint-test fakes + image endpoint `?redacted=1`

**Files:**
- Modify: `backend/app/routers/documents.py:13` (imports), `:396-437` (`get_image`)
- Test: `backend/tests/test_redacted_rendition.py` (new)

**Interfaces:**
- Consumes: `burn_page` from Task 1.
- Produces: `GET /api/documents/{doc_id}/image/{page_num}?redacted=1` burns that page's redactions and returns a JPEG `Response`; flag off keeps today's `FileResponse`/storage paths. Also produces the `FakeSession`/`FakeResult`/`FakeDoc`/`FakeUser`/`FakeRedaction` test fakes Tasks 3–5 reuse (they live at the top of `backend/tests/test_redacted_rendition.py`).

- [ ] **Step 1: Write the failing tests (including the shared fakes)**

Create `backend/tests/test_redacted_rendition.py`:

```python
"""Fake-session unit tests for the ?redacted=1 rendition (P1-2/3). No DB/network.

Same pattern as tests/test_redaction_endpoints.py: call the async router
functions directly with a fake session + monkeypatched deps.
"""

import asyncio
import io
import re
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.responses import FileResponse
from PIL import Image

import app.routers.documents as dd

_TS = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


class FakeUser:
    def __init__(self, uid="u1"):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"
        self.display_name = uid


class FakeDoc:
    def __init__(self, doc_id, image_paths, production_id=1, text_content="secret words"):
        self.id = doc_id
        self.production_id = production_id
        self.image_paths = image_paths
        self.page_count = len(image_paths)
        self.bates_begin = "DOC-000001"
        self.bates_end = "DOC-000002"
        self.title = None
        self.summary = None
        self.processing_status = "complete"
        self.metadata_ = {}
        self.text_content = text_content
        self.native_path = None
        self.tags = []


class FakeRedaction:
    def __init__(self, page_num=1, x_pct=10.0, y_pct=10.0, w_pct=40.0, h_pct=30.0,
                 reason_code="pii"):
        self.page_num = page_num
        self.x_pct = x_pct
        self.y_pct = y_pct
        self.w_pct = w_pct
        self.h_pct = h_pct
        self.reason_code = reason_code


class FakeAnnotation:
    def __init__(self, page_num=1):
        self.page_num = page_num
        self.x_pct = 50.0
        self.y_pct = 50.0
        self.color = "blue"
        self.content = "a note"
        self.created_by = "u1"
        self.created_at = _TS


class FakeResult:
    def __init__(self, items=None, scalar=None):
        self._items = items or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._items

    def scalar(self):
        return self._scalar


class FakeSession:
    """Dispatches execute() by table name in the compiled statement."""

    def __init__(self, docs=None, redactions=None, annotations=None):
        self._docs = docs or {}
        self.redactions = redactions or []
        self.annotations = annotations or []

    async def get(self, model, key):
        if model.__name__ == "Document":
            return self._docs.get(key)
        return None  # User lookups in the pdf path fall back to uid

    async def execute(self, stmt):
        sql = str(stmt)
        if "FROM redactions" in sql:
            if "count(" in sql:
                return FakeResult(scalar=len(self.redactions))
            return FakeResult(items=self.redactions)
        if "FROM annotations" in sql:
            if "count(" in sql:
                return FakeResult(scalar=len(self.annotations))
            return FakeResult(items=self.annotations)
        if "FROM notes" in sql:
            return FakeResult(scalar=0)
        return FakeResult()


def _patch_access(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    monkeypatch.setattr(dd, "get_accessible_production_ids", fake_accessible)


def _page_jpeg(tmp_path, name="page1.jpg", size=(400, 400)):
    p = tmp_path / name
    Image.new("RGB", size, "white").save(p, "JPEG", quality=95)
    return str(p)


# --- image endpoint -------------------------------------------------------

def test_image_flag_off_returns_fileresponse_unchanged(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path)])
    db = FakeSession(docs={doc_id: doc}, redactions=[FakeRedaction()])
    out = asyncio.run(dd.get_image(doc_id=doc_id, page_num=1, w=None, redacted=False,
                                   db=db, user=FakeUser()))
    assert isinstance(out, FileResponse)


def test_image_redacted_burns_black_box(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path)])
    db = FakeSession(docs={doc_id: doc}, redactions=[FakeRedaction(x_pct=10, y_pct=10, w_pct=40, h_pct=30)])
    out = asyncio.run(dd.get_image(doc_id=doc_id, page_num=1, w=None, redacted=True,
                                   db=db, user=FakeUser()))
    img = Image.open(io.BytesIO(out.body))
    r, g, b = img.getpixel((120, 100))       # center of the box (30%, 25%) on 400px
    assert r < 40 and g < 40 and b < 40      # JPEG-tolerant "black"
    r, g, b = img.getpixel((380, 380))       # far outside
    assert r > 200 and g > 200 and b > 200


def test_image_redacted_no_redactions_falls_back_to_normal(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path)])
    db = FakeSession(docs={doc_id: doc}, redactions=[])
    out = asyncio.run(dd.get_image(doc_id=doc_id, page_num=1, w=None, redacted=True,
                                   db=db, user=FakeUser()))
    assert isinstance(out, FileResponse)


def test_image_redacted_only_burns_matching_page(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path)])
    # redaction is on page 2; we request page 1 -> FakeSession returns rows
    # filtered by the endpoint's WHERE, which we emulate by giving no rows
    db = FakeSession(docs={doc_id: doc}, redactions=[])
    out = asyncio.run(dd.get_image(doc_id=doc_id, page_num=1, w=None, redacted=True,
                                   db=db, user=FakeUser()))
    assert isinstance(out, FileResponse)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redacted_rendition.py -q`
Expected: FAIL with `TypeError: get_image() got an unexpected keyword argument 'redacted'`

- [ ] **Step 3: Implement**

In `backend/app/routers/documents.py`:

(a) line 13, add `Redaction` to the models import:

```python
from app.models import Annotation, Document, DocumentTag, Note, Redaction, User
```

(b) after line 26 (`from app.schemas import ...`), add:

```python
from app.services.redaction_render import burn_page
```

(c) replace `get_image` (lines 396–437) with:

```python
@router.get("/{doc_id}/image/{page_num}")
async def get_image(
    doc_id: UUID,
    page_num: int,
    w: int | None = Query(None, ge=50, le=2000, description="Resize width for thumbnails"),
    redacted: bool = Query(False, description="Burn redactions into the returned image"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if page_num < 1 or page_num > len(doc.image_paths):
        raise HTTPException(status_code=404, detail="Page not found")
    raw_path = doc.image_paths[page_num - 1]
    if not raw_path:
        raise HTTPException(status_code=404, detail="Image file not found")

    rects = []
    if redacted:
        result = await db.execute(
            select(Redaction).where(
                Redaction.document_id == doc_id, Redaction.page_num == page_num
            )
        )
        rects = list(result.scalars().all())

    if raw_path.startswith("productions/"):
        from app.services.storage import get_download_bytes
        try:
            data = get_download_bytes(raw_path)
        except Exception:
            raise HTTPException(status_code=404, detail="Image file not found in storage")
        if w or rects:
            import io
            from PIL import Image as PILImage
            img = PILImage.open(io.BytesIO(data))
            if rects:
                img = burn_page(img, rects)
            if w:
                ratio = w / img.width
                new_h = int(img.height * ratio)
                img = img.resize((w, new_h), PILImage.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=75)
            data = buf.getvalue()
        return Response(content=data, media_type="image/jpeg")
    else:
        path = Path(raw_path.replace("\\", "/")).resolve()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Image file not found")
        if rects:
            import io
            from PIL import Image as PILImage
            img = burn_page(PILImage.open(str(path)), rects)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            return Response(content=buf.getvalue(), media_type="image/jpeg")
        return FileResponse(str(path), media_type="image/jpeg")
```

Note the flag-off path is untouched: no redaction query runs, local files still stream via `FileResponse`, and the storage-path `w` resize behaves as before (burn happens before resize).

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redacted_rendition.py backend\tests\test_redaction_render.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/documents.py backend/tests/test_redacted_rendition.py
git commit -m "feat(p1-2/3): ?redacted=1 burns boxes into page image endpoint"
```

---

### Task 3: PDF endpoint `?redacted=1` — burn pages, drop pins and index

**Files:**
- Modify: `backend/app/routers/documents.py:440-460` (signature) and `:511-531` (annotation load)
- Test: `backend/tests/test_redacted_rendition.py` (append)

**Interfaces:**
- Consumes: `burn_page` (Task 1), fakes from Task 2.
- Produces: `GET /api/documents/{doc_id}/pdf?redacted=1` returns a PDF whose pages have redactions burned in, with no annotation pins and no annotation index pages.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_redacted_rendition.py`:

```python
# --- pdf endpoint ---------------------------------------------------------

def _pdf_page_count(pdf_bytes: bytes) -> int:
    # Pillow writes one "/Type /Page" object per page plus one "/Type /Pages" tree.
    return len(re.findall(rb"/Type /Page[^s]", pdf_bytes))


def _first_embedded_jpeg(pdf_bytes: bytes) -> Image.Image:
    start = pdf_bytes.index(b"\xff\xd8")
    end = pdf_bytes.index(b"\xff\xd9", start) + 2
    return Image.open(io.BytesIO(pdf_bytes[start:end]))


def test_pdf_flag_off_keeps_annotation_index(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path)])
    db = FakeSession(docs={doc_id: doc}, annotations=[FakeAnnotation()],
                     redactions=[FakeRedaction()])
    out = asyncio.run(dd.get_document_pdf(doc_id=doc_id, redacted=False,
                                          db=db, user=FakeUser()))
    assert _pdf_page_count(out.body) == 2  # 1 page + 1 annotation index page


def test_pdf_redacted_drops_pins_and_index(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path)])
    db = FakeSession(docs={doc_id: doc}, annotations=[FakeAnnotation()],
                     redactions=[FakeRedaction(x_pct=10, y_pct=10, w_pct=40, h_pct=30)])
    out = asyncio.run(dd.get_document_pdf(doc_id=doc_id, redacted=True,
                                          db=db, user=FakeUser()))
    assert _pdf_page_count(out.body) == 1  # no index page
    page = _first_embedded_jpeg(out.body)
    r, g, b = page.getpixel((int(page.width * 0.3), int(page.height * 0.25)))
    assert r < 40 and g < 40 and b < 40  # burned box
    r, g, b = page.getpixel((int(page.width * 0.95), int(page.height * 0.95)))
    assert r > 200 and g > 200 and b > 200


def test_pdf_redacted_no_redactions_is_clean_document(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path)])
    db = FakeSession(docs={doc_id: doc}, annotations=[FakeAnnotation()], redactions=[])
    out = asyncio.run(dd.get_document_pdf(doc_id=doc_id, redacted=True,
                                          db=db, user=FakeUser()))
    assert _pdf_page_count(out.body) == 1  # still as-produced: no annotations
    page = _first_embedded_jpeg(out.body)
    r, g, b = page.getpixel((int(page.width * 0.3), int(page.height * 0.25)))
    assert r > 200 and g > 200 and b > 200  # nothing burned
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redacted_rendition.py -q -k pdf`
Expected: FAIL with `TypeError: get_document_pdf() got an unexpected keyword argument 'redacted'`

- [ ] **Step 3: Implement**

In `backend/app/routers/documents.py`, `get_document_pdf`:

(a) signature (line ~441) becomes:

```python
@router.get("/{doc_id}/pdf")
async def get_document_pdf(
    doc_id: UUID,
    redacted: bool = Query(False, description="As-produced rendition: burn redactions, omit annotations"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate a multi-page PDF from the document's page images.

    Default: annotations burned in as numbered pins plus an annotation index
    appended at the end. With redacted=1: the as-produced view — redaction
    boxes burned in, no pins, no index.
    """
```

(b) replace the annotation-loading block (currently lines 511–531, from `# Load annotations and resolve author display names once.` through the `author_names` loop) with:

```python
    annotations: list[Annotation] = []
    if redacted:
        # As-produced: burn redaction boxes; annotations are work product
        # and are omitted entirely (no pins, no index pages).
        red_result = await db.execute(
            select(Redaction).where(Redaction.document_id == doc.id)
        )
        red_by_page: dict[int, list[Redaction]] = {}
        for r in red_result.scalars().all():
            red_by_page.setdefault(r.page_num, []).append(r)
        loaded = [
            (idx, burn_page(img, red_by_page[idx]) if idx in red_by_page else img)
            for idx, img in loaded
        ]
    else:
        ann_result = await db.execute(
            select(Annotation)
            .where(Annotation.document_id == doc.id)
            .order_by(Annotation.page_num, Annotation.created_at)
        )
        annotations = list(ann_result.scalars().all())

    by_page: dict[int, list[Annotation]] = {}
    for a in annotations:
        by_page.setdefault(a.page_num, []).append(a)

    author_names: dict[str, str] = {}
    if annotations:
        unique_ids = {a.created_by for a in annotations}
        for uid in unique_ids:
            u = await db.get(User, uid)
            if u:
                author_names[uid] = u.display_name or u.email or uid
            else:
                author_names[uid] = uid
```

Everything downstream already no-ops when `annotations` is empty: the pin loop iterates `by_page` (empty) and `_build_index_pages()` returns `[]` immediately.

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redacted_rendition.py backend\tests\test_redaction_render.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/documents.py backend/tests/test_redacted_rendition.py
git commit -m "feat(p1-2/3): ?redacted=1 pdf burns boxes, omits pins and annotation index"
```

---

### Task 4: Text endpoint `?redacted=1` — withhold when redactions exist

**Files:**
- Modify: `backend/app/routers/documents.py:1031-1043` (`get_text`)
- Test: `backend/tests/test_redacted_rendition.py` (append)

**Interfaces:**
- Consumes: fakes from Task 2; `Redaction` import from Task 2.
- Produces: `GET /api/documents/{doc_id}/text?redacted=1` → `{"text": "", "withheld": true}` iff the doc has ≥1 redaction, else `{"text": <normal>, "withheld": false}`. Flag off: today's `{"text": <normal>}` exactly.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_redacted_rendition.py`:

```python
# --- text endpoint --------------------------------------------------------

def test_text_flag_off_unchanged(monkeypatch):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, ["p1.jpg"], text_content="secret words")
    db = FakeSession(docs={doc_id: doc}, redactions=[FakeRedaction()])
    out = asyncio.run(dd.get_text(doc_id=doc_id, redacted=False, db=db, user=FakeUser()))
    assert out == {"text": "secret words"}


def test_text_redacted_withholds_when_redactions_exist(monkeypatch):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, ["p1.jpg"], text_content="secret words")
    db = FakeSession(docs={doc_id: doc}, redactions=[FakeRedaction()])
    out = asyncio.run(dd.get_text(doc_id=doc_id, redacted=True, db=db, user=FakeUser()))
    assert out == {"text": "", "withheld": True}


def test_text_redacted_passes_through_without_redactions(monkeypatch):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, ["p1.jpg"], text_content="secret words")
    db = FakeSession(docs={doc_id: doc}, redactions=[])
    out = asyncio.run(dd.get_text(doc_id=doc_id, redacted=True, db=db, user=FakeUser()))
    assert out == {"text": "secret words", "withheld": False}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redacted_rendition.py -q -k text`
Expected: FAIL with `TypeError: get_text() got an unexpected keyword argument 'redacted'`

- [ ] **Step 3: Implement**

Replace `get_text` (lines 1031–1043) with:

```python
@router.get("/{doc_id}/text")
async def get_text(
    doc_id: UUID,
    redacted: bool = Query(False, description="Withhold extracted text if the document has redactions"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if redacted:
        # Flat text_content has no word coordinates, so region-level removal
        # is impossible — the as-produced text for a redacted doc is withheld
        # entirely (re-OCR of burned images is Phase 2).
        count = (await db.execute(
            select(func.count(Redaction.id)).where(Redaction.document_id == doc_id)
        )).scalar() or 0
        if count:
            return {"text": "", "withheld": True}
        return {"text": doc.text_content or "", "withheld": False}
    return {"text": doc.text_content or ""}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redacted_rendition.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/documents.py backend/tests/test_redacted_rendition.py
git commit -m "feat(p1-2/3): ?redacted=1 text endpoint withholds extracted text"
```

---

### Task 5: `redaction_count` on DocumentDetail

**Files:**
- Modify: `backend/app/schemas.py:154-171` (`DocumentDetail`), `backend/app/routers/documents.py:1084-1109` (`_doc_detail`)
- Test: `backend/tests/test_redacted_rendition.py` (append)

**Interfaces:**
- Consumes: fakes from Task 2.
- Produces: `DocumentDetail.redaction_count: int` (default 0), populated by `_doc_detail` with a count query — same pattern as `annotation_count`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_redacted_rendition.py`:

```python
# --- detail payload -------------------------------------------------------

def test_doc_detail_includes_redaction_count(monkeypatch):
    doc_id = uuid4()
    doc = FakeDoc(doc_id, ["p1.jpg"])
    db = FakeSession(docs={doc_id: doc},
                     redactions=[FakeRedaction(), FakeRedaction(page_num=2)])
    detail = asyncio.run(dd._doc_detail(doc, db))
    assert detail.redaction_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redacted_rendition.py -q -k detail`
Expected: FAIL — `AttributeError: 'DocumentDetail' object has no attribute 'redaction_count'` (or count == 0 if the field is added first; the assertion must fail before implementation either way).

- [ ] **Step 3: Implement**

(a) `backend/app/schemas.py`, in `DocumentDetail` after `annotation_count: int = 0` (line 169), add:

```python
    redaction_count: int = 0
```

(b) `backend/app/routers/documents.py`, in `_doc_detail`, after the `annotation_count` query (line ~1091), add:

```python
    redaction_count = (await db.execute(
        select(func.count(Redaction.id)).where(Redaction.document_id == doc.id)
    )).scalar() or 0
```

and add `redaction_count=redaction_count,` to the `DocumentDetail(...)` constructor call after `annotation_count=annotation_count,`.

- [ ] **Step 4: Run test to verify it passes**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redacted_rendition.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas.py backend/app/routers/documents.py backend/tests/test_redacted_rendition.py
git commit -m "feat(p1-2/3): redaction_count on DocumentDetail"
```

---

### Task 6: Full-suite verification + PR

**Files:** none new.

**Interfaces:** n/a — verification gate.

- [ ] **Step 1: Run the entire backend test suite**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests -q`
Expected: everything passes (baseline before this work: all green). Any failure in a pre-existing test means a flag-off behavior regression — fix the endpoint code, never the old test.

- [ ] **Step 2: Verify migration-import safety is untouched**

No migration was added and `redaction_render.py` is not imported by any migration — confirm with:
`git diff origin/main --stat -- backend/alembic`
Expected: empty output.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/p1-2-3-redacted-rendition
gh pr create --title "feat(p1-2/3): redacted rendition — burn-in + as-produced view" --body "$(cat <<'EOF'
## Summary
- Pure `burn_page()` service (black box + reason-code label) for reuse by Phase 2 production output
- Opt-in `?redacted=1` on image/pdf/text document endpoints (as-produced view; pdf drops annotation pins + index; text withheld when redactions exist)
- `redaction_count` on DocumentDetail

Spec: docs/superpowers/specs/2026-07-21-p1-2-3-redacted-rendition-design.md

## Test plan
- [x] Pixel-level burn_page tests (black inside, untouched outside, label fit, no mutation)
- [x] Endpoint tests: flag-off unchanged, burn applied, pins/index dropped, text withheld iff redactions exist
- [x] Full backend suite green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
