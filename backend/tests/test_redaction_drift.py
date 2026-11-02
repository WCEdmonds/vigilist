"""Render only what was locked: redactions that changed after lock block it."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.services.redaction_drift import redaction_drift
from tests.fakes import FakeResult, FakeSession

LOCKED = datetime(2026, 11, 1, 12, 0)


def _ps():
    return SimpleNamespace(id=1, locked_at=LOCKED)


def _item(doc, bates, disposition="redact_in_part", count=2, native=False):
    return SimpleNamespace(document_id=doc, bates_begin=bates, disposition=disposition,
                           redaction_count=count, produce_native=native)


def _run(items, rows):
    db = FakeSession(responders=[("FROM redactions", FakeResult(rows=rows))])
    return asyncio.run(redaction_drift(db, _ps(), items))


def test_clean_set_has_no_drift():
    d = uuid4()
    assert _run([_item(d, "ACME000001")], [(d, 2, LOCKED - timedelta(hours=1))]) == []


def test_added_after_lock():
    d = uuid4()
    assert _run([_item(d, "ACME000001")], [(d, 3, LOCKED + timedelta(minutes=5))]) == ["ACME000001"]


def test_edited_after_lock_same_count():
    d = uuid4()
    assert _run([_item(d, "ACME000001")], [(d, 2, LOCKED + timedelta(minutes=5))]) == ["ACME000001"]


def test_deleted_after_lock_caught_by_count():
    d = uuid4()
    assert _run([_item(d, "ACME000001")], [(d, 1, LOCKED - timedelta(hours=1))]) == ["ACME000001"]


def test_all_deleted_after_lock():
    d = uuid4()
    assert _run([_item(d, "ACME000001")], []) == ["ACME000001"]


def test_redactions_on_a_produce_item():
    d = uuid4()
    items = [_item(d, "ACME000001", disposition="produce", count=None)]
    assert _run(items, [(d, 1, LOCKED - timedelta(hours=1))]) == ["ACME000001"]


def test_pre_release_item_without_snapshot_skips_count_check():
    d = uuid4()
    assert _run([_item(d, "ACME000001", count=None)], [(d, 1, LOCKED - timedelta(hours=1))]) == []


def test_overridden_produce_with_unchanged_redactions_is_not_drift():
    """I-4: an audited 'produce' override of a redacted doc renders as overridden."""
    d = uuid4()
    items = [_item(d, "ACME000001", disposition="produce", count=2)]
    assert _run(items, [(d, 2, LOCKED - timedelta(hours=1))]) == []


def test_redactions_added_to_a_produce_item_after_lock_are_drift():
    d = uuid4()
    items = [_item(d, "ACME000001", disposition="produce", count=0)]
    assert _run(items, [(d, 1, LOCKED + timedelta(minutes=1))]) == ["ACME000001"]
