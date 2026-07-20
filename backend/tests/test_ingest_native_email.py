"""Unit tests for email→Documents structuring (SP4b-1). Pure, no DB/storage."""

import hashlib

from app.services.email_parse import ParsedMessage
from app.services.extractors import ExtractResult
from app.services.ingest_native import build_email_documents


def _fake_extract(filename, data, ocr_fn=None):
    return ExtractResult(text=f"text-of-{filename}", file_type="csv", extraction_status="ok")


def test_parent_and_attachment_share_family_and_have_distinct_controls():
    parsed = ParsedMessage(
        from_="alice@example.com",
        to="bob@example.com",
        cc="carol@example.com",
        bcc="",
        subject="Q3 numbers",
        date_sent="Mon, 20 Jul 2026 14:30:00 +0000",
        body_text="See attached.",
        attachments=[("numbers.csv", b"col1,col2\n1,2\n")],
    )
    docs = build_email_documents(
        parsed,
        message_control="PREFIX 000123",
        production_id=7,
        source_path="mail/inbox/email.eml",
        custodian="Alice",
        msg_bytes=b"the-raw-message-bytes",
        extract_fn=_fake_extract,
        ocr_fn=None,
    )

    assert len(docs) == 2
    parent, child = docs

    # Parent
    assert parent.bates_begin == "PREFIX 000123"
    assert parent.bates_end == "PREFIX 000123"
    assert parent.family_id == "PREFIX 000123"
    assert parent.email_from == "alice@example.com"
    assert parent.email_to == "bob@example.com"
    assert parent.email_cc == "carol@example.com"
    assert parent.email_subject == "Q3 numbers"
    assert parent.date_sent is not None  # normalize_date parsed the RFC-822 date
    assert parent.text_content == "See attached."
    assert parent.file_type == "email"
    assert parent.custodian == "Alice"
    assert parent.source_path == "mail/inbox/email.eml"
    assert parent.file_hash_sha256 == hashlib.sha256(b"the-raw-message-bytes").hexdigest()

    # Child attachment shares the family, gets a distinct control number
    assert child.family_id == "PREFIX 000123"
    assert child.bates_begin == "PREFIX 000123 .0001"
    assert child.bates_end == "PREFIX 000123 .0001"
    assert child.file_name == "numbers.csv"
    assert child.text_content == "text-of-numbers.csv"
    assert child.custodian == "Alice"
    assert child.file_hash_sha256 == hashlib.sha256(b"col1,col2\n1,2\n").hexdigest()


def test_parent_carries_native_path_for_retry_dedup_children_do_not():
    # The parent must carry the container's storage path in native_path so the
    # batch dedup query skips an already-ingested container on a Cloud Tasks
    # retry; children leave native_path None (parent gates the container).
    parsed = ParsedMessage(
        from_="a@x.com",
        subject="hi",
        body_text="body",
        attachments=[("a.txt", b"1")],
    )
    docs = build_email_documents(
        parsed,
        message_control="PREFIX 000042",
        production_id=3,
        source_path="mail/x.eml",
        custodian=None,
        msg_bytes=b"raw",
        native_path="productions/3/raw/mail/x.eml",
        extract_fn=_fake_extract,
    )
    parent, child = docs
    assert parent.native_path == "productions/3/raw/mail/x.eml"
    assert child.native_path is None


def test_message_with_no_attachments_yields_only_parent():
    parsed = ParsedMessage(from_="a@x.com", subject="hi", body_text="body")
    docs = build_email_documents(
        parsed,
        message_control="PREFIX 000001",
        production_id=1,
        source_path="a.eml",
        custodian=None,
        msg_bytes=b"raw",
        extract_fn=_fake_extract,
    )
    assert len(docs) == 1
    assert docs[0].family_id == "PREFIX 000001"
    assert docs[0].email_subject == "hi"


def test_multiple_attachments_get_sequential_control_numbers():
    parsed = ParsedMessage(
        from_="a@x.com",
        subject="two files",
        body_text="body",
        attachments=[("one.txt", b"1"), ("two.txt", b"2")],
    )
    docs = build_email_documents(
        parsed,
        message_control="PREFIX 000005 -0002",
        production_id=1,
        source_path="c.pst",
        custodian=None,
        msg_bytes=b"raw",
        extract_fn=_fake_extract,
    )
    assert [d.bates_begin for d in docs] == [
        "PREFIX 000005 -0002",
        "PREFIX 000005 -0002 .0001",
        "PREFIX 000005 -0002 .0002",
    ]
    assert all(d.family_id == "PREFIX 000005 -0002" for d in docs)
