"""Imaging state on documents and the lock-time redaction snapshot."""

from uuid import uuid4

from app.models import Document, ProductionSetItem
from app.schemas import DocumentDetail


def test_document_detail_imaging_defaults():
    d = DocumentDetail(id=uuid4(), production_id=1, bates_begin="ACME 1", bates_end="ACME 1",
                       page_count=1, metadata={}, text_content=None, native_path=None, image_paths=[])
    assert d.imaging_status is None and d.imaging_error is None
    assert d.imageable is False and d.image_count == 0


def test_models_have_imaging_columns():
    for col in ("imaging_status", "imaging_error", "imaged_from", "header_regions", "imaging_requested_at"):
        assert col in Document.__table__.c, col
    assert "redaction_count" in ProductionSetItem.__table__.c
