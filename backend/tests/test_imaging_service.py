"""Imaging: turn emails, PDF and image attachments into page images + word
boxes on demand, so they can be redacted."""

import asyncio
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import fitz
import pytest
from PIL import Image

import app.services.imaging as im
import app.services.ingest_pdf as ip
from app.services.ocr import PageOcr
from tests.fakes import FakeResult, FakeSession

SENT = datetime(2026, 3, 2, 3, 30, tzinfo=timezone.utc)


def _doc(**kw):
    base = dict(id=uuid4(), production_id=4, bates_begin="ACME 000004 -0001",
                family_id="ACME 000004 -0001", file_type="email", native_path="productions/4/raw/box.pst",
                image_paths=[], ocr_paths=[], page_count=1,
                email_from="alice@acme.example", email_to="bob@acme.example", email_cc=None,
                email_bcc=None, email_subject="RE: ACME", date_sent=SENT, date_received=None,
                email_body_html=None, text_content="Hello ACME.",
                imaging_status=None, imaging_error=None, imaged_from=None, header_regions=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def sink(monkeypatch):
    stored = {}
    monkeypatch.setattr(ip, "upload_bytes", lambda data, path, content_type=None: stored.__setitem__(path, data))
    return stored


def _ocr(jpeg):
    return PageOcr(text="scan", words=[{"t": "scan", "x": 1, "y": 1, "w": 5, "h": 2, "c": 0.9}],
                   width=100, height=100)


def _pdf(pages):
    d = fitz.open()
    for i in range(pages):
        d.new_page().insert_text((72, 72), f"ACME page {i + 1}")
    return d.tobytes()


def test_imaging_kind_table():
    assert im.imaging_kind(_doc()) == "email"
    att = dict(file_type="pdf", family_id="ACME 1")
    assert im.imaging_kind(_doc(native_path="productions/4/attachments/x/budget.pdf", **att)) == "pdf"
    assert im.imaging_kind(_doc(native_path="productions/4/attachments/x/scan.PNG", **att)) == "image"
    assert im.imaging_kind(_doc(native_path="productions/4/attachments/x/budget.xlsx", **att)) is None
    assert im.imaging_kind(_doc(image_paths=["productions/4/converted/a.jpg"])) is None
    assert im.imaging_kind(_doc(file_type="pdf", native_path=None)) is None


def test_build_pages_email_uploads_index_aligned_assets(sink):
    images, ocrs, count, regions = im.build_pages(_doc(), "email", ["budget.xlsx"], ocr_fn=_ocr)
    assert count == len(images) == len(ocrs) >= 1
    assert all(p for p in images) and all(p for p in ocrs)
    assert set(sink) == set(images) | set(ocrs)
    assert all(k.startswith(("productions/4/converted/", "productions/4/ocr/")) for k in sink)
    assert "subject" in regions and "attachments" in regions
    words = json.loads(sink[ocrs[0]])["words"]
    assert any(w["t"] == "Subject:" for w in words)


def test_build_pages_pdf_uses_native_pages(sink, monkeypatch):
    monkeypatch.setattr(im, "get_download_bytes", lambda path: _pdf(3))
    doc = _doc(file_type="pdf", native_path="productions/4/attachments/x/budget.pdf",
               bates_begin="ACME 000004 -0001 .0001")
    images, ocrs, count, regions = im.build_pages(doc, "pdf", [], ocr_fn=_ocr)
    assert count == 3 and len(images) == 3 and regions is None


def test_build_pages_multiframe_tiff(sink, monkeypatch):
    frames = [Image.new("RGB", (200, 260), c) for c in ("white", "gray")]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    monkeypatch.setattr(im, "get_download_bytes", lambda path: buf.getvalue())
    doc = _doc(file_type="tiff", native_path="productions/4/attachments/x/scan.tif",
               bates_begin="ACME 000004 -0001 .0002")
    images, ocrs, count, _ = im.build_pages(doc, "image", [], ocr_fn=_ocr)
    assert count == 2 and len(ocrs) == 2 and all(ocrs)


def _session(doc, redactions=0):
    return FakeSession(
        get_objects={("Document", doc.id): doc},
        responders=[
            ("count(redactions.id)", FakeResult(scalar=redactions)),
            ("documents.file_name", FakeResult(rows=[])),
        ],
    )


def test_image_document_refuses_when_redactions_exist(sink):
    doc = _doc()
    with pytest.raises(im.ImagingRefused):
        asyncio.run(im.image_document(_session(doc, redactions=2), doc.id))
    assert doc.imaging_status == "failed" and "frozen" in doc.imaging_error
    assert sink == {}


def test_image_document_records_failure(monkeypatch):
    doc = _doc()

    def boom(*a, **kw):
        raise RuntimeError("readpst exploded")

    monkeypatch.setattr(im, "build_pages", boom)
    asyncio.run(im.image_document(_session(doc), doc.id))
    assert doc.imaging_status == "failed" and "readpst exploded" in doc.imaging_error


def test_image_document_commits_before_heavy_work_and_writes_results(monkeypatch):
    doc = _doc()
    db = _session(doc)
    seen = {}

    def fake_build(d, kind, names, *, ocr_fn):
        seen["commits_before"] = db.commits
        seen["status_during"] = d.imaging_status
        return (["productions/4/converted/a.jpg"], ["productions/4/ocr/a.json"], 1, {"subject": {}})

    monkeypatch.setattr(im, "build_pages", fake_build)
    asyncio.run(im.image_document(db, doc.id))
    assert seen == {"commits_before": 1, "status_during": "running"}
    assert doc.imaging_status == "done" and doc.imaging_error is None
    assert doc.image_paths == ["productions/4/converted/a.jpg"] and doc.page_count == 1
    assert doc.imaged_from == "email" and doc.header_regions == {"subject": {}}
    assert db.commits == 2
