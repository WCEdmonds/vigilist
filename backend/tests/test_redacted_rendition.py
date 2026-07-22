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
from fastapi import HTTPException
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
        self.executed = []

    async def get(self, model, key):
        if model.__name__ == "Document":
            return self._docs.get(key)
        return None  # User lookups in the pdf path fall back to uid

    async def execute(self, stmt):
        sql = str(stmt)
        self.executed.append(sql)
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


def test_image_redacted_query_filters_by_page(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path)])
    db = FakeSession(docs={doc_id: doc}, redactions=[FakeRedaction(page_num=2)])
    asyncio.run(dd.get_image(doc_id=doc_id, page_num=1, w=None, redacted=True,
                             db=db, user=FakeUser()))
    red_sql = [s for s in db.executed if "FROM redactions" in s]
    assert red_sql and "page_num" in red_sql[0]


def test_image_redacted_storage_path_burns_before_resize(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    import app.services.storage as storage
    buf = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(buf, "JPEG", quality=95)
    monkeypatch.setattr(storage, "get_download_bytes", lambda p: buf.getvalue())
    doc_id = uuid4()
    doc = FakeDoc(doc_id, ["productions/pages/p1.jpg"])
    db = FakeSession(docs={doc_id: doc},
                     redactions=[FakeRedaction(x_pct=10, y_pct=10, w_pct=40, h_pct=30)])
    out = asyncio.run(dd.get_image(doc_id=doc_id, page_num=1, w=200, redacted=True,
                                   db=db, user=FakeUser()))
    img = Image.open(io.BytesIO(out.body))
    assert img.width == 200                      # resize applied after burn
    r, g, b = img.getpixel((60, 50))             # box center scales with resize
    assert r < 40 and g < 40 and b < 40
    r, g, b = img.getpixel((190, 190))
    assert r > 200 and g > 200 and b > 200


def test_image_redacted_unreadable_local_file_404(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    p = tmp_path / "bad.jpg"
    p.write_bytes(b"not a jpeg")
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [str(p)])
    db = FakeSession(docs={doc_id: doc}, redactions=[FakeRedaction()])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dd.get_image(doc_id=doc_id, page_num=1, w=None, redacted=True,
                                 db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_image_redacted_unreadable_storage_bytes_404(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    import app.services.storage as storage
    monkeypatch.setattr(storage, "get_download_bytes", lambda p: b"not a jpeg")
    doc_id = uuid4()
    doc = FakeDoc(doc_id, ["productions/pages/p1.jpg"])
    db = FakeSession(docs={doc_id: doc}, redactions=[FakeRedaction()])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dd.get_image(doc_id=doc_id, page_num=1, w=None, redacted=True,
                                 db=db, user=FakeUser()))
    assert exc.value.status_code == 404


# --- pdf endpoint ---------------------------------------------------------

def _pdf_page_count(pdf_bytes: bytes) -> int:
    # Pillow writes one "/Type /Page" object per page plus one "/Type /Pages" tree.
    return len(re.findall(rb"/Type /Page[^s]", pdf_bytes))


def _embedded_jpegs(pdf_bytes: bytes) -> list[Image.Image]:
    imgs = []
    pos = 0
    while True:
        start = pdf_bytes.find(b"\xff\xd8", pos)
        if start == -1:
            break
        end = pdf_bytes.find(b"\xff\xd9", start)
        if end == -1:
            break
        imgs.append(Image.open(io.BytesIO(pdf_bytes[start:end + 2])))
        pos = end + 2
    return imgs


def _first_embedded_jpeg(pdf_bytes: bytes) -> Image.Image:
    return _embedded_jpegs(pdf_bytes)[0]


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


def test_pdf_redacted_multipage_burns_only_redacted_page(monkeypatch, tmp_path):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, [_page_jpeg(tmp_path, "p1.jpg"), _page_jpeg(tmp_path, "p2.jpg")])
    db = FakeSession(docs={doc_id: doc},
                     redactions=[FakeRedaction(page_num=2, x_pct=10, y_pct=10, w_pct=40, h_pct=30)])
    out = asyncio.run(dd.get_document_pdf(doc_id=doc_id, redacted=True, db=db, user=FakeUser()))
    assert _pdf_page_count(out.body) == 2
    pages = _embedded_jpegs(out.body)
    assert len(pages) == 2
    r, g, b = pages[0].getpixel((int(pages[0].width * 0.3), int(pages[0].height * 0.25)))
    assert r > 200 and g > 200 and b > 200      # page 1 untouched
    r, g, b = pages[1].getpixel((int(pages[1].width * 0.3), int(pages[1].height * 0.25)))
    assert r < 40 and g < 40 and b < 40         # page 2 burned
    assert "_redacted.pdf" in out.headers["content-disposition"]


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


def test_text_redacted_count_query_scoped_to_document(monkeypatch):
    _patch_access(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, ["p1.jpg"])
    db = FakeSession(docs={doc_id: doc}, redactions=[FakeRedaction()])
    asyncio.run(dd.get_text(doc_id=doc_id, redacted=True, db=db, user=FakeUser()))
    count_sql = [s for s in db.executed if "FROM redactions" in s and "count(" in s]
    assert count_sql and "document_id" in count_sql[0]


# --- detail payload -------------------------------------------------------

def test_doc_detail_includes_redaction_count(monkeypatch):
    doc_id = uuid4()
    doc = FakeDoc(doc_id, ["p1.jpg"])
    db = FakeSession(docs={doc_id: doc},
                     redactions=[FakeRedaction(), FakeRedaction(page_num=2)])
    detail = asyncio.run(dd._doc_detail(doc, db))
    assert detail.redaction_count == 2
