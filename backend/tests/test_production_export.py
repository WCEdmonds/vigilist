"""Fake-session tests for production export assembly (P2-3). No DB/GCS."""

import asyncio
import io
import zipfile
from uuid import uuid4

import app.services.production_export as pe
from tests.fakes import TS, FakeResult, FakeSession


class FakePS:
    def __init__(self, **kw):
        self.id = kw.get("set_id", 1)
        self.production_id = kw.get("production_id", 1)
        self.name = "Vol 1"
        self.status = kw.get("status", "locked")
        self.prefix = kw.get("prefix", "SMITH")
        self.padding = 6
        self.start_number = kw.get("start_number", 1)
        self.designation = kw.get("designation", None)
        self.locked_at = TS
        self.render_status = kw.get("render_status", "rendered")
        self.rendered_at = TS
        self.package_status = kw.get("package_status", "packaging")
        self.package_error = None
        self.package_path = None
        self.packaged_at = None


class FakeItem:
    def __init__(self, document_id, bates_begin, bates_end, pages,
                 disposition="produce", **kw):
        self.document_id = document_id
        self.bates_begin = bates_begin
        self.bates_end = bates_end
        self.pages = pages
        self.disposition = disposition
        self.designation = kw.get("designation", None)
        self.output_path = kw.get("output_path", f"productions/1/x/{bates_begin}.pdf")
        self.sort_order = kw.get("sort_order", 1)


class FakeDoc:
    def __init__(self, doc_id, **kw):
        self.id = doc_id
        self.family_id = kw.get("family_id", None)
        self.custodian = kw.get("custodian", "T. Owner")
        self.email_from = kw.get("email_from", "a@x.com")
        self.email_to = kw.get("email_to", "b@y.com")
        self.email_cc = kw.get("email_cc", None)
        self.email_subject = kw.get("email_subject", "Secret subject")
        self.date_sent = kw.get("date_sent", TS)
        self.date_received = None
        self.file_name = kw.get("file_name", "mail.eml")
        self.file_type = kw.get("file_type", "eml")
        self.file_hash_md5 = "md5x"
        self.file_hash_sha256 = "shax"
        self.text_content = kw.get("text_content", "hello text")


def test_package_path_for():
    assert pe.package_path_for(FakePS()) == \
        "productions/1/production_sets/1/package/SMITH_production.zip"


def test_build_dat_rows_values_and_family_ranges():
    d1, d2, d3 = uuid4(), uuid4(), uuid4()
    items = [
        FakeItem(d1, "SMITH000001", "SMITH000002", 2, "produce", sort_order=1),
        FakeItem(d2, "SMITH000003", "SMITH000003", 1, "redact_in_part", sort_order=2),
        FakeItem(d3, "SMITH000004", "SMITH000004", 1, "withhold", sort_order=3),
    ]
    docs = [FakeDoc(d1, family_id="F1"), FakeDoc(d2, family_id="F1"),
            FakeDoc(d3)]
    db = FakeSession(responders=[("FROM documents", FakeResult(items=docs))])
    rows = asyncio.run(pe.build_dat_rows(db, FakePS(designation="CONF"), items))
    r1, r2, r3 = rows
    # family F1 spans docs 1-2
    assert (r1["BEGATTACH"], r1["ENDATTACH"]) == ("SMITH000001", "SMITH000003")
    assert (r2["BEGATTACH"], r2["ENDATTACH"]) == ("SMITH000001", "SMITH000003")
    assert (r3["BEGATTACH"], r3["ENDATTACH"]) == ("SMITH000004", "SMITH000004")
    assert r1["TEXTPATH"] == ".\\TEXT\\SMITH000001.txt"
    assert r2["TEXTPATH"] == ""            # redacted: never ship stored text
    assert r2["REDACTED"] == "Y"
    assert r3["WITHHELD"] == "Y"
    assert r3["SUBJECT"] == "" and r3["FILENAME"] == ""  # privilege safety
    assert r1["SUBJECT"] == "Secret subject"
    assert r1["CONFIDENTIALITY"] == "CONF"
    assert r1["DATESENT"] == "2026-07-22"


def test_compute_manifest_counts_and_continuity():
    d1, d2 = uuid4(), uuid4()
    items = [FakeItem(d1, "SMITH000001", "SMITH000002", 2, "produce"),
             FakeItem(d2, "SMITH000005", "SMITH000005", 1, "withhold")]
    m = pe.compute_manifest(FakePS(), items)
    assert m["counts"] == {"documents": 2, "pages": 3, "produce": 1,
                           "redact_in_part": 0, "withhold": 1}
    assert m["bates_range"] == {"begin": "SMITH000001", "end": "SMITH000005"}
    assert m["continuity"]["ok"] is False  # gap 000003-000004
    assert m["artifacts"][0]["path"].endswith("SMITH000001.pdf")


def test_package_set_happy_path(monkeypatch):
    d1, d2 = uuid4(), uuid4()
    items = [FakeItem(d1, "SMITH000001", "SMITH000001", 1, "produce", sort_order=1),
             FakeItem(d2, "SMITH000002", "SMITH000002", 1, "withhold", sort_order=2)]
    docs = [FakeDoc(d1), FakeDoc(d2)]
    ps = FakePS()
    captured = {}

    def fake_download(path):
        return b"%PDF-fake"

    def fake_upload(local_path, remote_path, content_type=None):
        with open(local_path, "rb") as f:
            captured["zip"] = f.read()
        captured["remote"] = (remote_path, content_type)
        return remote_path

    monkeypatch.setattr(pe.storage, "get_download_bytes", fake_download)
    monkeypatch.setattr(pe.storage, "upload_file", fake_upload)
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("FROM production_set_items", FakeResult(items=items)),
            ("FROM documents", FakeResult(items=docs)),
        ],
    )
    asyncio.run(pe.package_set(db, 1))
    assert ps.package_status == "packaged"
    assert ps.package_path == pe.package_path_for(ps)
    zf = zipfile.ZipFile(io.BytesIO(captured["zip"]))
    names = set(zf.namelist())
    assert names == {"DATA/SMITH.dat", "DATA/SMITH.opt",
                     "PDFS/SMITH000001.pdf", "PDFS/SMITH000002.pdf",
                     "TEXT/SMITH000001.txt", "manifest.json"}
    manifest = zf.read("manifest.json").decode()
    assert "sha256" in manifest
    assert captured["remote"] == (ps.package_path, "application/zip")


def test_package_set_missing_artifact_marks_error(monkeypatch):
    d1 = uuid4()
    items = [FakeItem(d1, "SMITH000001", "SMITH000001", 1, "produce")]
    ps = FakePS()

    def boom(path):
        raise RuntimeError("404 from GCS")

    monkeypatch.setattr(pe.storage, "get_download_bytes", boom)
    monkeypatch.setattr(pe.storage, "upload_file", lambda *a, **k: None)
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("FROM production_set_items", FakeResult(items=items)),
            ("FROM documents", FakeResult(items=[FakeDoc(d1)])),
        ],
    )
    asyncio.run(pe.package_set(db, 1))
    assert ps.package_status == "error"
    assert "SMITH000001" in ps.package_error


def test_package_set_requires_rendered():
    ps = FakePS(render_status="rendering")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    asyncio.run(pe.package_set(db, 1))
    assert ps.package_status == "error"
