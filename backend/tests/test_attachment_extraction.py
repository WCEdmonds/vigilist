import io

import fitz
import pytest
from PIL import Image

from app.services.email_parse import ParsedMessage
from app.services.extractors import extract_attachment, extract_pdf_text, should_ocr_image
from app.services.ingest_native import build_email_documents


@pytest.fixture
def png_bytes_factory():
    def make(target_bytes, size):
        import os

        w, h = size
        im = Image.frombytes("RGB", size, os.urandom(w * h * 3)) if target_bytes > 20_000 else Image.new("RGB", size, "white")
        buf = io.BytesIO()
        im.save(buf, "PNG")
        data = buf.getvalue()
        if target_bytes <= 20_000:
            assert len(data) < 20_000
        return data

    return make


@pytest.fixture
def two_page_pdf_one_scanned():
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "typed page text for the first page")
    doc.new_page()  # blank: treated as scanned
    data = doc.tobytes()
    doc.close()
    return data


def test_tiny_and_inline_images_skip_ocr(png_bytes_factory):
    assert not should_ocr_image("logo.png", png_bytes_factory(10_000, (400, 400)), inline=False)
    assert not should_ocr_image("sig.png", png_bytes_factory(50_000, (600, 120)), inline=False)
    assert not should_ocr_image("scan.png", png_bytes_factory(500_000, (2000, 2000)), inline=True)
    assert should_ocr_image("scan.png", png_bytes_factory(500_000, (2000, 2000)), inline=False)


def test_unreadable_large_image_is_ocrd():
    assert should_ocr_image("x.png", b"\0" * 30_000, inline=False)


def test_build_email_documents_does_not_ocr_tiny_inline_image(png_bytes_factory):
    calls = []
    parsed = ParsedMessage(
        subject="s", body_text="b",
        attachments=[("image001.png", png_bytes_factory(10_000, (400, 400)))],
        inline_names={"image001.png"},
    )
    docs = build_email_documents(
        parsed, message_control="ACME 000001", production_id=1,
        source_path="m/a.eml", custodian=None, msg_bytes=b"raw",
        ocr_fn=lambda b: calls.append(1) or "TEXT",
    )
    assert len(docs) == 2
    assert docs[1].extraction_status == "ok"
    assert calls == []


def test_extract_attachment_routes_big_image_to_ocr(png_bytes_factory):
    res = extract_attachment(
        "scan.png", png_bytes_factory(500_000, (800, 800)), inline=False, ocr_fn=lambda b: "HELLO"
    )
    assert res.text == "HELLO"


def test_pdf_attachment_gets_text_layer_and_ocr_only_blank_pages(two_page_pdf_one_scanned):
    ocr_calls = []
    res = extract_pdf_text(two_page_pdf_one_scanned, ocr_fn=lambda img: ocr_calls.append(1) or "SCANNED TEXT")
    assert "typed page text" in res.text and "SCANNED TEXT" in res.text and len(ocr_calls) == 1
    assert res.file_type == "pdf"


def test_ocr_failure_on_one_page_keeps_other_pages():
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "first page typed text")
    doc.new_page()  # scanned
    doc.new_page().insert_text((72, 72), "third page typed text")
    data = doc.tobytes()
    doc.close()

    def boom(img):
        raise RuntimeError("vision down")

    res = extract_pdf_text(data, ocr_fn=boom)
    assert "first page typed text" in res.text and "third page typed text" in res.text
    assert res.extraction_status == "partial"
    assert "page 2" in res.extraction_error


def test_parse_eml_marks_cid_referenced_attachment_inline():
    from email.message import EmailMessage

    from app.services.email_parse import _parse_eml_bytes

    m = EmailMessage()
    m["From"] = "a@example.com"
    m["To"] = "b@example.com"
    m["Subject"] = "hi"
    m.set_content("plain")
    m.add_alternative('<html><body><img src="cid:logo1"></body></html>', subtype="html")
    html_part = m.get_payload()[1]
    html_part.add_related(b"PNG-logo", "image", "png", cid="<logo1>", filename="logo.png")
    m.add_attachment(b"%PDF-fake", maintype="application", subtype="pdf", filename="real.pdf")

    parsed = _parse_eml_bytes(m.as_bytes())
    names = [n for n, _ in parsed.attachments]
    assert "logo.png" in names and "real.pdf" in names
    assert parsed.inline_names == {"logo.png"}
