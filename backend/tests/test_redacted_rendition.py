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
