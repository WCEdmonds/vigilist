"""Backfill: recover already-ingested attachments from their stored mailbox."""

import hashlib

from app.services.email_parse import ParsedMessage
from scripts.backfill_attachment_natives import (
    attachment_index,
    locate_attachment_bytes,
)


def _sha(b):
    return hashlib.sha256(b).hexdigest()


MSGS = [
    ParsedMessage(subject="first", attachments=[("a.txt", b"A1")]),
    ParsedMessage(subject="second", attachments=[("x.docx", b"X"), ("y.pdf", b"Y")]),
]


def test_finds_an_attachment_by_its_hash_wherever_it_sits():
    # Re-expanding a PST does not reproduce ingest's message order (positions
    # drifted by a few messages on the real mailbox), so position is no guide.
    index = attachment_index(MSGS)
    assert locate_attachment_bytes(index, _sha(b"Y")) == ("y.pdf", b"Y")
    assert locate_attachment_bytes(index, _sha(b"A1")) == ("a.txt", b"A1")


def test_unknown_hash_returns_none():
    assert locate_attachment_bytes(attachment_index(MSGS), _sha(b"nope")) is None


def test_missing_stored_hash_is_not_trusted():
    assert locate_attachment_bytes(attachment_index(MSGS), None) is None


def test_identical_bytes_in_two_messages_index_once():
    msgs = MSGS + [ParsedMessage(subject="fwd", attachments=[("copy.pdf", b"Y")])]
    assert locate_attachment_bytes(attachment_index(msgs), _sha(b"Y"))[1] == b"Y"
