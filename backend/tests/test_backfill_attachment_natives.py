"""Backfill: recover already-ingested attachments from their stored mailbox."""

import hashlib

from app.services.email_parse import ParsedMessage
from scripts.backfill_attachment_natives import locate_attachment_bytes, message_and_attachment_index


def _sha(b):
    return hashlib.sha256(b).hexdigest()


MSGS = [
    ParsedMessage(subject="first", attachments=[("a.txt", b"A1")]),
    ParsedMessage(subject="second", attachments=[("x.docx", b"X"), ("y.pdf", b"Y")]),
]


def test_indexes_from_control_numbers():
    assert message_and_attachment_index("ACME 000004 -0776", "ACME 000004 -0776 .0002") == (776, 2)
    assert message_and_attachment_index("ACME 000009", "ACME 000009 .0001") == (1, 1)
    assert message_and_attachment_index("ACME 000009", "ACME 000010 .0001") is None
    assert message_and_attachment_index("ACME 000009", "ACME 000009") is None


def test_locates_the_right_attachment_and_checks_its_hash():
    name, data = locate_attachment_bytes(MSGS, "M -0002", "M -0002 .0002", _sha(b"Y"))
    assert (name, data) == ("y.pdf", b"Y")


def test_hash_mismatch_or_out_of_range_returns_none():
    assert locate_attachment_bytes(MSGS, "M -0002", "M -0002 .0002", _sha(b"nope")) is None
    assert locate_attachment_bytes(MSGS, "M -0003", "M -0003 .0001", _sha(b"A1")) is None
    assert locate_attachment_bytes(MSGS, "M -0001", "M -0001 .0005", _sha(b"A1")) is None


def test_missing_stored_hash_is_not_trusted():
    assert locate_attachment_bytes(MSGS, "M -0001", "M -0001 .0001", None) is None
