"""Unit tests for alias-only backfill derivation (no DB, no network)."""

from app.services.metadata_normalize import backfill_typed_fields


def test_backfill_derives_typed_fields_from_metadata():
    meta = {"Custodian": "Doe, J", "Date Sent": "03/04/2025", "Widget": "x"}
    typed = backfill_typed_fields(meta)
    assert typed["custodian"] == "Doe, J"
    assert typed["date_sent"].year == 2025
    assert "Widget" not in typed


def test_backfill_empty_when_nothing_recognized():
    assert backfill_typed_fields({"Widget": "x"}) == {}
