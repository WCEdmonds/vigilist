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
