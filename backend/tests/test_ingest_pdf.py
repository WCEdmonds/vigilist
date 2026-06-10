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


from app.services import ingest_pdf as pdf_mod
from app.services.ingest_pdf import list_pdf_sources, process_pdf_record


def test_list_pdf_sources_sorts_and_keeps_relative_path(monkeypatch):
    raw = [
        "productions/7/raw/B/second.pdf",
        "productions/7/raw/A/first.PDF",
        "productions/7/raw/notes.txt",
        "productions/7/raw/A/skip.opt",
    ]
    monkeypatch.setattr(pdf_mod, "list_files", lambda prefix: raw)

    items = list_pdf_sources(7)

    # Only PDFs, case-insensitive, sorted by storage path
    assert [i["storage_path"] for i in items] == [
        "productions/7/raw/A/first.PDF",
        "productions/7/raw/B/second.pdf",
    ]
    assert items[0]["relative_path"] == "A/first.PDF"
    assert items[0]["filename"] == "first.PDF"


def test_process_pdf_record_assembles_document(monkeypatch):
    item = {
        "storage_path": "productions/7/raw/A/first.pdf",
        "relative_path": "A/first.pdf",
        "filename": "first.pdf",
    }

    monkeypatch.setattr(pdf_mod, "get_download_bytes", lambda path: b"%PDF-fake")
    monkeypatch.setattr(
        pdf_mod,
        "render_and_extract_pdf",
        lambda pdf_bytes, ocr_fn, dpi=pdf_mod.RENDER_DPI: (
            [b"\xff\xd8jpeg1", b"\xff\xd8jpeg2"],
            "extracted text",
            2,
        ),
    )
    uploaded = []
    monkeypatch.setattr(
        pdf_mod,
        "upload_bytes",
        lambda data, remote, content_type=None: uploaded.append(remote) or remote,
    )

    errors: list[str] = []
    doc = process_pdf_record(
        production_id=7,
        item=item,
        global_index=0,
        prefix="SMITH",
        errors=errors,
    )

    assert doc.bates_begin == "SMITH 000001"
    assert doc.bates_end == "SMITH 000001"
    assert doc.page_count == 2
    assert doc.title == "first"
    assert doc.text_content == "extracted text"
    assert doc.metadata_["File Name"] == "first.pdf"
    assert doc.metadata_["Folder"] == "A"
    assert doc.native_path == "productions/7/raw/A/first.pdf"
    assert len(doc.image_paths) == 2
    assert len(uploaded) == 2
    assert errors == []
