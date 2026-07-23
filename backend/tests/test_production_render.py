"""Fake-session tests for the production render pipeline (P2-2). No DB/GCS."""

import asyncio
from uuid import uuid4

import pytest
from PIL import Image

import app.services.production_render as pr
from tests.fakes import FakeResult, FakeSession

GRAY = (60, 60, 60)


class FakePS:
    def __init__(self, set_id=1, production_id=1, **kw):
        self.id = set_id
        self.production_id = production_id
        self.status = kw.get("status", "locked")
        self.prefix = kw.get("prefix", "SMITH")
        self.padding = kw.get("padding", 6)
        self.designation = kw.get("designation", None)
        self.image_format = kw.get("image_format", "pdf")
        self.native_file_types = kw.get("native_file_types", [])
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
        self.produce_native = kw.get("produce_native", False)


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


# --- P2-5: TIFF + native slip-sheets ---------------------------------------

def test_render_item_tiff_uploads_per_page(monkeypatch):
    uploads, burns = _spies(monkeypatch)
    doc_id = uuid4()
    item = FakeItem(doc_id, disposition="produce")
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    out = asyncio.run(pr.render_item(db, FakePS(image_format="tiff"), item))
    assert len(uploads) == 2
    assert all(ct == "image/tiff" for _, ct, _ in uploads)
    assert out == "productions/1/production_sets/1/tiff/SMITH000001.tif"
    assert item.output_path == out


def test_render_item_native_uses_native_slipsheet(monkeypatch):
    uploads, burns = _spies(monkeypatch)
    titles = []
    real_slip = pr.slip_sheet

    def spy(bates, designation, title="DOCUMENT WITHHELD"):
        titles.append(title)
        return real_slip(bates, designation, title)

    monkeypatch.setattr(pr, "slip_sheet", spy)
    doc_id = uuid4()
    item = FakeItem(doc_id, disposition="produce", produce_native=True, pages=1)
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    asyncio.run(pr.render_item(db, FakePS(), item))
    assert titles == ["PRODUCED IN NATIVE FORMAT"]
    assert burns == []
    assert len(uploads) == 1
