"""Regression test for the final-review Finding 1 fix (P1-6): a standalone
image upload processed via ``process_native_record`` — the exact call site
named in the finding (native image uploads feeding ``extract(ocr_fn=...)``)
— must not break when the underlying Vision OCR path returns the layout-aware
``PageOcr`` contract.

Pure, no DB/storage: ``get_download_bytes`` and the real Vision entry point
(``app.services.ocr.ocr_page_vision_bytes``) are monkeypatched; everything
else is the real code path (``process_native_record`` -> ``extract`` ->
``_ocr_jpeg_text`` -> ``_ocr_jpeg`` -> ``ocr_page_vision_bytes``), so a
regression that lets a PageOcr leak into ``extract()``'s str-only ocr_fn
fails with an AttributeError instead of silently passing.
"""

import app.services.ingest_native as ingest_native_mod
import app.services.ocr as ocr_mod
from app.services.ocr import PageOcr


def test_standalone_image_upload_survives_pageocr_returning_vision_call(monkeypatch):
    monkeypatch.setattr(
        ingest_native_mod, "get_download_bytes", lambda path: b"\xff\xd8\xff-fake-jpeg"
    )
    monkeypatch.setattr(ocr_mod, "ocr_page_vision_bytes", lambda b: PageOcr(text="RECOVERED"))

    item = {
        "storage_path": "productions/7/raw/scan.jpg",
        "relative_path": "scan.jpg",
        "filename": "scan.jpg",
    }
    errors: list[str] = []
    doc = ingest_native_mod.process_native_record(
        custodian=None,
        production_id=7,
        item=item,
        global_index=0,
        prefix="SMITH",
        errors=errors,
    )

    assert doc is not None
    assert errors == []
    assert doc.text_content == "RECOVERED"
    assert doc.extraction_status == "ok"
    assert doc.extraction_status != "error"
    assert doc.file_type == "image"
