"""Unit tests for MBOX container expansion (Task F2). No network, no readpst.

Mirrors the PST/eml test patterns in test_email_parse.py: MBOX messages must
flow through the SAME `_parse_eml_bytes` pipeline PST-exploded messages use,
so threading headers, attachments, and body decoding all behave identically.
"""

import mailbox
import os
import tempfile

import pytest

from app.services.email_parse import ParsedMessage, expand_email


def _build_mbox_bytes(messages: list[bytes]) -> bytes:
    """Write the given raw RFC-822 messages into a real mbox file and return its bytes."""
    fd, path = tempfile.mkstemp(suffix=".mbox")
    os.close(fd)
    os.remove(path)  # mailbox.mbox creates the file itself
    try:
        box = mailbox.mbox(path)
        try:
            box.lock()
            for raw in messages:
                box.add(mailbox.mboxMessage(raw))
            box.flush()
        finally:
            box.unlock()
            box.close()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        if os.path.exists(path):
            os.remove(path)
        lock_path = path + ".lock"
        if os.path.exists(lock_path):
            os.remove(lock_path)


def _msg_one() -> bytes:
    return (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: First message\r\n"
        b"Date: Mon, 20 Jul 2026 14:30:00 +0000\r\n"
        b"Message-ID: <msg-1@example.com>\r\n"
        b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
        b"\r\n"
        b"Hello from message one.\r\n"
    )


def _msg_two() -> bytes:
    return (
        b"From: bob@example.com\r\n"
        b"To: alice@example.com\r\n"
        b"Subject: Re: First message\r\n"
        b"Date: Mon, 20 Jul 2026 15:00:00 +0000\r\n"
        b"Message-ID: <msg-2@example.com>\r\n"
        b"In-Reply-To: <msg-1@example.com>\r\n"
        b"References: <msg-1@example.com>\r\n"
        b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
        b"\r\n"
        b"Reply to message one.\r\n"
    )


def test_expand_email_mbox_returns_two_messages_with_distinct_subjects():
    data = _build_mbox_bytes([_msg_one(), _msg_two()])
    msgs = expand_email("archive.mbox", data)
    assert len(msgs) == 2
    assert all(isinstance(m, ParsedMessage) for m in msgs)
    subjects = {m.subject for m in msgs}
    assert subjects == {"First message", "Re: First message"}


def test_expand_email_mbox_preserves_threading_headers():
    data = _build_mbox_bytes([_msg_one(), _msg_two()])
    msgs = expand_email("archive.mbox", data)
    by_subject = {m.subject: m for m in msgs}
    first = by_subject["First message"]
    reply = by_subject["Re: First message"]
    assert first.message_id == "<msg-1@example.com>"
    assert first.in_reply_to == ""
    assert reply.message_id == "<msg-2@example.com>"
    assert reply.in_reply_to == "<msg-1@example.com>"
    assert reply.references == "<msg-1@example.com>"


def test_expand_email_mbox_preserves_body_text():
    data = _build_mbox_bytes([_msg_one(), _msg_two()])
    msgs = expand_email("archive.mbox", data)
    by_subject = {m.subject: m for m in msgs}
    assert "Hello from message one." in by_subject["First message"].body_text
    assert "Reply to message one." in by_subject["Re: First message"].body_text


def test_expand_email_mbox_single_message():
    data = _build_mbox_bytes([_msg_one()])
    msgs = expand_email("single.mbox", data)
    assert len(msgs) == 1
    assert msgs[0].subject == "First message"


def test_expand_email_mbox_empty_container_returns_empty_list():
    data = _build_mbox_bytes([])
    msgs = expand_email("empty.mbox", data)
    assert msgs == []


def test_expand_email_mbox_bad_bytes_returns_empty_list_never_raises():
    # Garbage bytes are not a valid mbox at all; mailbox.mbox parsing must not
    # raise out of expand_email — the never-raises contract applies to mbox too.
    msgs = expand_email("broken.mbox", b"\x00\x01not-an-mbox-at-all")
    assert msgs == []


def test_mbox_extension_not_confused_with_other_containers():
    assert expand_email("mystery.dat", b"whatever") == []
