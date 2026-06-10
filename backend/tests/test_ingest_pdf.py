import fitz  # PyMuPDF

from app.services.ingest_pdf import derive_bates_prefix, render_and_extract_pdf


def test_prefix_uppercases_first_token():
    assert derive_bates_prefix("Smith Loose Docs") == "SMITH"


def test_prefix_strips_punctuation():
    assert derive_bates_prefix("smith-jones, llp") == "SMITHJONES"


def test_prefix_truncates_to_12_chars():
    assert derive_bates_prefix("Supercalifragilistic Matter") == "SUPERCALIFRA"


def test_prefix_falls_back_to_doc_when_empty():
    assert derive_bates_prefix("!!! ???") == "DOC"
    assert derive_bates_prefix("") == "DOC"


def _born_digital_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _blank_two_page_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_born_digital_uses_embedded_text_and_skips_ocr():
    ocr_calls = []

    def fake_ocr(jpeg_bytes: bytes) -> str:
        ocr_calls.append(jpeg_bytes)
        return "SHOULD-NOT-BE-USED"

    pages, text, page_count = render_and_extract_pdf(
        _born_digital_pdf("Hello discovery"), ocr_fn=fake_ocr
    )

    assert page_count == 1
    assert len(pages) == 1
    assert pages[0][:3] == b"\xff\xd8\xff"  # JPEG magic bytes
    assert "Hello discovery" in text
    assert ocr_calls == []  # OCR not invoked for born-digital text


def test_scanned_page_falls_back_to_ocr():
    def fake_ocr(jpeg_bytes: bytes) -> str:
        return "OCR-RECOVERED-TEXT"

    pages, text, page_count = render_and_extract_pdf(
        _blank_two_page_pdf(), ocr_fn=fake_ocr
    )

    assert page_count == 2
    assert len(pages) == 2
    assert text.count("OCR-RECOVERED-TEXT") == 2


def test_pages_rendered_for_every_page():
    pages, _text, page_count = render_and_extract_pdf(
        _blank_two_page_pdf(), ocr_fn=lambda b: ""
    )
    assert page_count == len(pages) == 2
