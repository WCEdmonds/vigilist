"""Unit tests for the email container expander (SP4b-1). No network, no readpst."""

import shutil
from email.message import EmailMessage

import pytest

from app.services.email_parse import (
    ParsedMessage,
    _parse_eml_bytes,
    expand_email,
)


def _build_eml(with_attachment: bool = True) -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com, carol@example.com"
    msg["Cc"] = "dave@example.com"
    msg["Subject"] = "Q3 numbers"
    msg["Date"] = "Mon, 20 Jul 2026 14:30:00 +0000"
    msg.set_content("Please see the attached spreadsheet.\n")
    if with_attachment:
        msg.add_attachment(
            b"col1,col2\n1,2\n",
            maintype="text",
            subtype="csv",
            filename="numbers.csv",
        )
    return msg.as_bytes()


def test_parse_eml_headers_body_and_attachment():
    parsed = _parse_eml_bytes(_build_eml())
    assert isinstance(parsed, ParsedMessage)
    assert parsed.from_ == "alice@example.com"
    assert parsed.to == "bob@example.com, carol@example.com"
    assert parsed.cc == "dave@example.com"
    assert parsed.subject == "Q3 numbers"
    assert parsed.date_sent == "Mon, 20 Jul 2026 14:30:00 +0000"
    assert "attached spreadsheet" in parsed.body_text
    assert len(parsed.attachments) == 1
    name, blob = parsed.attachments[0]
    assert name == "numbers.csv"
    assert blob == b"col1,col2\n1,2\n"


def test_parse_eml_no_attachment():
    parsed = _parse_eml_bytes(_build_eml(with_attachment=False))
    assert parsed.attachments == []
    assert "attached spreadsheet" in parsed.body_text


def test_parse_eml_single_part_attachment_is_not_body_text():
    # A non-multipart message that is itself an attachment (an inline image, a
    # forwarded file) must go through the attachment path, not be decoded as body.
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["Subject"] = "photo"
    msg.set_content(
        b"\x89PNG\r\n\x1a\n\x00binary",
        maintype="image",
        subtype="png",
        disposition="attachment",
        filename="photo.png",
    )
    parsed = _parse_eml_bytes(msg.as_bytes())
    assert parsed.body_text == ""
    assert len(parsed.attachments) == 1
    name, blob = parsed.attachments[0]
    assert name == "photo.png"
    assert blob == b"\x89PNG\r\n\x1a\n\x00binary"


def test_expand_email_eml_returns_one_message():
    msgs = expand_email("email.eml", _build_eml())
    assert len(msgs) == 1
    assert msgs[0].subject == "Q3 numbers"


def test_expand_email_bad_bytes_returns_empty_list():
    # A malformed .msg makes extract-msg raise; expand_email must swallow it and
    # return []. (.eml parsing is lenient — the stdlib never raises on garbage —
    # so .msg is the extension that actually exercises the never-raises contract.)
    assert expand_email("broken.msg", b"\x00\x01not-an-email") == []


def test_expand_email_unknown_extension_returns_empty():
    assert expand_email("mystery.dat", b"whatever") == []


def test_expand_email_msg_roundtrip():
    extract_msg = pytest.importorskip("extract_msg")
    # extract-msg cannot easily *write* .msg files; parse a committed fixture instead.
    import os

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample.msg")
    if not os.path.exists(fixture):
        pytest.skip("no sample.msg fixture available")
    with open(fixture, "rb") as fh:
        data = fh.read()
    msgs = expand_email("sample.msg", data)
    assert len(msgs) == 1
    assert isinstance(msgs[0], ParsedMessage)
    assert msgs[0].subject  # a real .msg fixture has a subject


def _build_eml_with_threading() -> bytes:
    # Use raw bytes so the folded References header (with \r\n + space) is
    # preserved exactly — EmailMessage (new policy) rejects embedded CRLFs.
    return (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Re: Q3 numbers\r\n"
        b"Date: Mon, 20 Jul 2026 14:30:00 +0000\r\n"
        b"Message-ID: <msg-2@example.com>\r\n"
        b"In-Reply-To: <msg-1@example.com>\r\n"
        b"References: <msg-0@example.com>\r\n"
        b" <msg-1@example.com>\r\n"
        b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
        b"\r\n"
        b"See below.\r\n"
    )


def test_parse_eml_captures_threading_headers():
    parsed = _parse_eml_bytes(_build_eml_with_threading())
    assert parsed.message_id == "<msg-2@example.com>"
    assert parsed.in_reply_to == "<msg-1@example.com>"
    # References whitespace/newlines collapse to single-space-joined ids.
    assert parsed.references == "<msg-0@example.com> <msg-1@example.com>"


def test_parse_eml_without_threading_headers_yields_empty_strings():
    parsed = _parse_eml_bytes(_build_eml(with_attachment=False))
    assert parsed.message_id == ""
    assert parsed.in_reply_to == ""
    assert parsed.references == ""


@pytest.mark.skipif(shutil.which("readpst") is None, reason="readpst not installed")
def test_expand_email_pst_integration():
    import os

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample.pst")
    if not os.path.exists(fixture):
        pytest.skip("no sample.pst fixture available")
    with open(fixture, "rb") as fh:
        data = fh.read()
    msgs = expand_email("sample.pst", data)
    assert isinstance(msgs, list)
    assert all(isinstance(m, ParsedMessage) for m in msgs)
