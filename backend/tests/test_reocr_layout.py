"""Re-OCR endpoints write layout text + ocr_paths (P1-6)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import app.services.ingest as ingest_mod
from app.routers.ingest import reocr_batch_handler


def _doc():
    doc = MagicMock()
    doc.id = "d0000000-0000-0000-0000-000000000001"
    doc.production_id = 7
    doc.bates_begin = "SMITH 000001"
    doc.image_paths = ["productions/7/converted/SMITH_000001_0001.jpg"]
    return doc


def _db_returning(docs):
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = docs
    db = MagicMock()
    # first execute() = the SELECT; later calls = tsvector UPDATEs
    db.execute = AsyncMock(side_effect=[select_result, MagicMock(), MagicMock()])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def test_reocr_batch_writes_layout_text_and_ocr_paths(monkeypatch):
    doc = _doc()
    monkeypatch.setattr(
        ingest_mod,
        "ocr_pages_with_sidecars",
        lambda pid, paths, label, errors: (
            ["Structured\n\ntext"],
            ["productions/7/ocr/SMITH_000001_0001.json"],
        ),
    )
    db = _db_returning([doc])

    asyncio.run(reocr_batch_handler({"production_id": 7}, db=db, _verified=None))

    assert doc.text_content == "Structured\n\ntext"
    assert doc.ocr_paths == ["productions/7/ocr/SMITH_000001_0001.json"]
    assert db.execute.await_count == 2  # SELECT + tsvector UPDATE
    db.commit.assert_awaited()


def test_reocr_batch_skips_already_processed(monkeypatch):
    """A fully-OCR'd document is not re-sent to (billed) Cloud Vision."""
    doc = _doc()
    doc.ocr_paths = ["productions/7/ocr/SMITH_000001_0001.json"]
    doc.text_content = "existing text"
    called = MagicMock()
    monkeypatch.setattr(ingest_mod, "ocr_pages_with_sidecars", called)
    db = _db_returning([doc])

    result = asyncio.run(reocr_batch_handler({"production_id": 7}, db=db, _verified=None))

    called.assert_not_called()
    assert result["skipped"] == 1
    assert result["processed"] == 0
    assert doc.text_content == "existing text"


def test_reocr_batch_reprocesses_document_with_failed_page(monkeypatch):
    """A document with a failed page ("" sidecar) is not considered done."""
    doc = _doc()
    doc.image_paths = [
        "productions/7/converted/SMITH_000001_0001.jpg",
        "productions/7/converted/SMITH_000001_0002.jpg",
    ]
    doc.ocr_paths = ["productions/7/ocr/SMITH_000001_0001.json", ""]  # page 2 failed
    monkeypatch.setattr(
        ingest_mod,
        "ocr_pages_with_sidecars",
        lambda pid, paths, label, errors: (
            ["Recovered"],
            [
                "productions/7/ocr/SMITH_000001_0001.json",
                "productions/7/ocr/SMITH_000001_0002.json",
            ],
        ),
    )
    db = _db_returning([doc])

    result = asyncio.run(reocr_batch_handler({"production_id": 7}, db=db, _verified=None))

    assert result["processed"] == 1
    assert result["skipped"] == 0
    assert doc.ocr_paths[1] == "productions/7/ocr/SMITH_000001_0002.json"


def test_reocr_batch_force_reprocesses_complete_doc(monkeypatch):
    """force=True re-OCRs even a fully-processed document."""
    doc = _doc()
    doc.ocr_paths = ["productions/7/ocr/SMITH_000001_0001.json"]
    called = MagicMock(
        return_value=(["Redone"], ["productions/7/ocr/SMITH_000001_0001.json"])
    )
    monkeypatch.setattr(ingest_mod, "ocr_pages_with_sidecars", called)
    db = _db_returning([doc])

    result = asyncio.run(
        reocr_batch_handler({"production_id": 7, "force": True}, db=db, _verified=None)
    )

    called.assert_called_once()
    assert result["processed"] == 1
    assert result["skipped"] == 0


def test_reocr_batch_blank_pages_still_persist_ocr_paths(monkeypatch):
    """No text recovered: ocr_paths still recorded, text_content untouched."""
    doc = _doc()
    doc.text_content = "old text"
    monkeypatch.setattr(
        ingest_mod,
        "ocr_pages_with_sidecars",
        lambda pid, paths, label, errors: ([], ["productions/7/ocr/SMITH_000001_0001.json"]),
    )
    db = _db_returning([doc])

    asyncio.run(reocr_batch_handler({"production_id": 7}, db=db, _verified=None))

    assert doc.text_content == "old text"
    assert doc.ocr_paths == ["productions/7/ocr/SMITH_000001_0001.json"]
    assert db.execute.await_count == 1  # SELECT only, no tsvector UPDATE
    db.commit.assert_awaited()
