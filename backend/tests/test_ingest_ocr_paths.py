"""Tests for the shared page-OCR + sidecar helper (P1-6)."""

import app.services.ocr as ocr_mod
import app.services.storage as storage_mod
from app.services.ingest import ocr_pages_with_sidecars
from app.services.ocr import PageOcr


def _happy_ocr(jpeg_bytes):
    return PageOcr(text="Page text", words=[{"t": "Page", "x": 1.0, "y": 1.0, "w": 5.0, "h": 2.0, "c": 0.9}], width=100, height=200)


def test_uploads_sidecar_per_page_and_aligns_paths(monkeypatch):
    uploaded = {}
    monkeypatch.setattr(storage_mod, "get_download_bytes", lambda p: b"jpeg")
    monkeypatch.setattr(
        storage_mod, "upload_bytes",
        lambda data, remote, content_type=None: uploaded.setdefault(remote, data) or remote,
    )
    monkeypatch.setattr(ocr_mod, "ocr_page_vision_bytes", _happy_ocr)

    errors: list[str] = []
    texts, ocr_paths = ocr_pages_with_sidecars(
        7,
        ["productions/7/converted/A_0001.jpg", "", "productions/7/converted/A_0003.jpg"],
        "SMITH 000001",
        errors,
    )

    assert texts == ["Page text", "Page text"]
    # "" jpeg placeholder stays "" in ocr_paths; alignment preserved
    assert ocr_paths == [
        "productions/7/ocr/A_0001.json",
        "",
        "productions/7/ocr/A_0003.json",
    ]
    assert set(uploaded) == {"productions/7/ocr/A_0001.json", "productions/7/ocr/A_0003.json"}
    assert errors == []


def test_ocr_failure_appends_error_and_empty_path(monkeypatch):
    def boom(jpeg_bytes):
        raise RuntimeError("Vision API error: quota")

    monkeypatch.setattr(storage_mod, "get_download_bytes", lambda p: b"jpeg")
    monkeypatch.setattr(ocr_mod, "ocr_page_vision_bytes", boom)

    errors: list[str] = []
    texts, ocr_paths = ocr_pages_with_sidecars(7, ["productions/7/converted/A_0001.jpg"], "SMITH 000001", errors)

    assert texts == []
    assert ocr_paths == [""]
    assert len(errors) == 1 and "SMITH 000001" in errors[0]


def test_sidecar_upload_failure_keeps_text_marks_path_empty(monkeypatch):
    def bad_upload(data, remote, content_type=None):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(storage_mod, "get_download_bytes", lambda p: b"jpeg")
    monkeypatch.setattr(storage_mod, "upload_bytes", bad_upload)
    monkeypatch.setattr(ocr_mod, "ocr_page_vision_bytes", _happy_ocr)

    errors: list[str] = []
    texts, ocr_paths = ocr_pages_with_sidecars(7, ["productions/7/converted/A_0001.jpg"], "SMITH 000001", errors)

    assert texts == ["Page text"]  # text survives a sidecar failure
    assert ocr_paths == [""]
    assert len(errors) == 1 and "sidecar" in errors[0]


def test_blank_page_still_writes_sidecar(monkeypatch):
    """OCR ran, found nothing: sidecar with words=[] distinguishes this from failure."""
    uploaded = []
    monkeypatch.setattr(storage_mod, "get_download_bytes", lambda p: b"jpeg")
    monkeypatch.setattr(
        storage_mod, "upload_bytes",
        lambda data, remote, content_type=None: uploaded.append(remote) or remote,
    )
    monkeypatch.setattr(
        ocr_mod, "ocr_page_vision_bytes",
        lambda b: PageOcr(text="", words=[], width=100, height=200),
    )

    errors: list[str] = []
    texts, ocr_paths = ocr_pages_with_sidecars(7, ["productions/7/converted/A_0001.jpg"], "SMITH 000001", errors)

    assert texts == []  # empty text not joined into text_content
    assert ocr_paths == ["productions/7/ocr/A_0001.json"]
    assert uploaded == ["productions/7/ocr/A_0001.json"]
