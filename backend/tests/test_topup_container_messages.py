"""PST top-up: find the messages a container holds that ingest never loaded
(readpst's parallel mode dropped ~3.5% of a real mailbox), and number them
after the container's existing messages."""

from datetime import datetime, timezone

import pytest

from app.services.email_parse import ParsedMessage
from scripts.topup_container_messages import (
    ExistingMessage,
    body_hash,
    container_base,
    existing_from_row,
    find_missing,
)

D1 = "Mon, 2 Mar 2026 03:30:00 +0000"
D1_DT = datetime(2026, 3, 2, 3, 30, tzinfo=timezone.utc)


def _existing(**kw):
    base = dict(message_id=None, date_sent=None, body=None, file_hash=None)
    base.update(kw)
    return ExistingMessage(
        message_id=base["message_id"], date_sent=base["date_sent"],
        body_hash=body_hash(base["body"]) if base["body"] is not None else None,
        file_hash=base["file_hash"],
    )


def test_message_id_match_counts_as_loaded():
    msgs = [ParsedMessage(message_id="<a@x>", subject="new subject decoding", body_text="b")]
    assert find_missing(msgs, [_existing(message_id="<a@x>")]) == []


def test_unmatched_message_is_missing():
    msgs = [ParsedMessage(message_id="<a@x>"), ParsedMessage(message_id="<b@x>")]
    assert [m.message_id for m in find_missing(msgs, [_existing(message_id="<a@x>")])] == ["<b@x>"]


def test_date_and_body_match_when_there_is_no_message_id():
    # Header decoding changed after ingest, so the serialized hash differs —
    # but the sent date and body text don't.
    msgs = [ParsedMessage(subject="=?utf-8?Q?caf=C3=A9?=", date_sent=D1, body_text="Hello\n")]
    assert find_missing(msgs, [_existing(date_sent=D1_DT, body="Hello")]) == []


def test_same_body_on_a_different_date_is_missing():
    msgs = [ParsedMessage(date_sent="Tue, 3 Mar 2026 03:30:00 +0000", body_text="Hello")]
    assert len(find_missing(msgs, [_existing(date_sent=D1_DT, body="Hello")])) == 1


def test_each_loaded_document_matches_only_once():
    # Two copies of one message in the PST (e.g. Inbox and a folder) but only
    # one loaded: the second copy is missing.
    msgs = [ParsedMessage(message_id="<a@x>"), ParsedMessage(message_id="<a@x>")]
    assert len(find_missing(msgs, [_existing(message_id="<a@x>")])) == 1


def test_serialized_hash_is_the_last_resort():
    from app.services.ingest_native import _serialize_message
    import hashlib

    m = ParsedMessage(subject="s", body_text="")  # no id, no date
    fh = hashlib.sha256(_serialize_message(m)).hexdigest()
    assert find_missing([m], [_existing(file_hash=fh)]) == []
    assert len(find_missing([m], [])) == 1


def test_container_base_and_next_number():
    base, nxt = container_base(["ACME 000004 -0001", "ACME 000004 -2325", "ACME 000004 -0017"])
    assert (base, nxt) == ("ACME 000004", 2326)


def test_container_base_refuses_mixed_or_single_message_numbering():
    with pytest.raises(ValueError):
        container_base(["ACME 000004 -0001", "ACME 000005 -0002"])
    with pytest.raises(ValueError):
        container_base(["ACME 000004"])


def test_existing_from_row_hashes_body():
    row = ("<a@x>", D1_DT, "Hello", "abc")
    e = existing_from_row(row)
    assert e.message_id == "<a@x>" and e.date_sent == D1_DT
    assert e.body_hash == body_hash("Hello  \n") and e.file_hash == "abc"
