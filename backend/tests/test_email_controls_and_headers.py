"""Second round from the Vote Thiru mbox ingest (2026-10-06).

1. compute_control_offset only matched plain '{PREFIX} NNNNNN' numbers, so in
   an email production (controls like 'VOTE 000001 -0001' and attachments
   'VOTE 000001 -0001 .0001') every later load restarted at 000001 and its
   whole container failed on uq_prod_bates.
2. Headers were stored as raw RFC 2047 encoded-words
   ('=?utf-8?Q?Mailchimp=20billing=20paused?=') instead of decoded text.
"""

from email.message import EmailMessage

from app.services.email_parse import _parse_eml_bytes, decode_header_value
from app.services.ingest import compute_control_offset


def test_offset_counts_email_and_attachment_controls():
    bates = ["VOTE 000001 -0001", "VOTE 000001 -0002 .0001", "VOTE 000013 -0342", "VOTE 000007"]
    assert compute_control_offset(bates, "VOTE") == 13


def test_offset_ignores_other_prefixes_and_junk():
    assert compute_control_offset(["VOTERS 000099", "VOTE abc", "XVOTE 000050"], "VOTE") == 0


def test_decode_header_value_handles_encoded_words():
    assert decode_header_value("=?utf-8?Q?Mailchimp=20billing=20paused?=") == "Mailchimp billing paused"
    assert decode_header_value(
        "=?utf-8?Q?No=20Reply=20=2D=20Mailchimp?= <no-reply@mailchimp.com>"
    ) == "No Reply - Mailchimp <no-reply@mailchimp.com>"
    assert decode_header_value("plain subject") == "plain subject"
    assert decode_header_value("") == ""


def test_decode_header_value_survives_bad_charset():
    assert decode_header_value("=?x-unknown?Q?caf=E9?=") != ""


def test_parsed_message_headers_are_decoded():
    msg = EmailMessage()
    msg["From"] = "Zoë Example <zoe@example.com>"
    msg["Subject"] = "Café meeting — notes"
    msg.set_content("body")
    raw = msg.as_bytes()
    assert b"=?utf-8?" in raw  # really encoded on the wire
    parsed = _parse_eml_bytes(raw)
    assert parsed.subject == "Café meeting — notes"
    assert parsed.from_ == "Zoë Example <zoe@example.com>"


def test_backfill_decodes_headers_and_derived_title():
    from types import SimpleNamespace

    from scripts.backfill_email_headers import decoded_updates

    raw = "=?utf-8?Q?Mailchimp=20billing=20paused?="
    doc = SimpleNamespace(email_subject=raw, email_from="a@b.c", email_to=None,
                          email_cc=None, email_bcc=None, title=raw)
    assert decoded_updates(doc) == {"email_subject": "Mailchimp billing paused",
                                    "title": "Mailchimp billing paused"}


def test_backfill_keeps_custom_titles_and_skips_clean_docs():
    from types import SimpleNamespace

    from scripts.backfill_email_headers import decoded_updates

    doc = SimpleNamespace(email_subject="=?utf-8?Q?Hi?=", email_from=None, email_to=None,
                          email_cc=None, email_bcc=None, title="Reviewer title")
    assert decoded_updates(doc) == {"email_subject": "Hi"}
    clean = SimpleNamespace(email_subject="Hi", email_from=None, email_to=None,
                            email_cc=None, email_bcc=None, title="Hi")
    assert decoded_updates(clean) == {}
