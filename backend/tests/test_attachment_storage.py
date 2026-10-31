"""Email attachments are stored so they can be previewed and produced natively.

Before: attachments got native_path=None and image_paths=[], so the viewer
showed "No viewable content" (customer report 2026-10-30).
"""

from email.message import EmailMessage

from app.services.email_parse import ParsedMessage
from app.services.extractors import ExtractResult
from app.services.ingest_native import (
    _expand_email_bytes,
    attachment_storage_path,
    build_email_documents,
)


def fake_extract(filename, data, ocr_fn=None):
    return ExtractResult(text=f"text-of-{filename}", file_type="docx", extraction_status="ok")


def _parsed(*atts):
    return ParsedMessage(from_="a@x.com", subject="Timeline", body_text="body", attachments=list(atts))


def test_storage_path_is_deterministic_sanitized_and_keeps_extension():
    p = attachment_storage_path(4, "ACME 000004 -0776 .0001", "Project Timeline.docx")
    assert p == "productions/4/attachments/ACME_000004_-0776_.0001/Project Timeline.docx"
    assert attachment_storage_path(4, "C .0002", "../../etc/passwd") == "productions/4/attachments/C_.0002/passwd"
    assert attachment_storage_path(4, "C .0003", "a<b>|c?.PDF").endswith("/a_b__c_.PDF")
    assert attachment_storage_path(4, "C .0004", "").endswith("/attachment")
    long = attachment_storage_path(4, "C .0005", "x" * 300 + ".xlsx")
    assert long.endswith(".xlsx") and len(long.rsplit("/", 1)[1]) <= 120
    assert "/raw/" not in p  # raw/ is scanned as new sources; never write there


def test_attachments_are_stored_and_linked():
    stored = []

    def store(data, path):
        stored.append((path, data))

    docs = build_email_documents(
        _parsed(("Timeline.docx", b"DOCX"), ("photo.jpg", b"JPG")),
        message_control="ACME 000004 -0776", production_id=4,
        source_path="mail/box.mbox", custodian=None, msg_bytes=b"raw",
        native_path="productions/4/raw/mail/box.mbox", extract_fn=fake_extract, store_fn=store,
    )
    parent, att1, att2 = docs
    assert parent.native_path == "productions/4/raw/mail/box.mbox"
    assert att1.native_path == "productions/4/attachments/ACME_000004_-0776_.0001/Timeline.docx"
    assert att2.native_path == "productions/4/attachments/ACME_000004_-0776_.0002/photo.jpg"
    assert stored == [(att1.native_path, b"DOCX"), (att2.native_path, b"JPG")]


def test_storage_failure_keeps_the_document():
    def store(data, path):
        raise OSError("bucket unavailable")

    docs = build_email_documents(
        _parsed(("Timeline.docx", b"DOCX")), message_control="M", production_id=4,
        source_path="m.eml", custodian=None, msg_bytes=b"raw", extract_fn=fake_extract, store_fn=store,
    )
    assert len(docs) == 2 and docs[1].native_path is None
    assert docs[1].text_content == "text-of-Timeline.docx"


def test_expand_email_bytes_passes_store_fn_through():
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["Subject"] = "hi"
    msg.set_content("body text")
    msg.add_attachment(b"hello", maintype="text", subtype="plain", filename="notes.txt")
    stored = []
    docs = _expand_email_bytes(
        "m.eml", msg.as_bytes(), base_control="E 000001", production_id=4,
        source_path="m.eml", custodian=None, errors=[], store_fn=lambda d, p: stored.append(p),
    )
    assert stored == ["productions/4/attachments/E_000001_.0001/notes.txt"]
    assert docs[1].native_path == stored[0]
