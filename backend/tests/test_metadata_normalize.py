"""Unit tests for metadata normalization + promotion (no DB, no network)."""

from datetime import timezone

from app.services.metadata_normalize import derive_file_type, normalize_date, promote_record


def test_normalize_date_iso_and_us_and_ampm():
    assert normalize_date("2026-07-16T13:45:00Z").tzinfo is not None
    d = normalize_date("07/16/2026 01:45 PM")
    assert (d.year, d.month, d.day, d.hour) == (2026, 7, 16, 13)
    assert normalize_date("07/16/2026").year == 2026
    assert normalize_date("") is None
    assert normalize_date("not a date") is None
    # stored UTC
    assert normalize_date("2026-07-16T13:45:00Z").astimezone(timezone.utc).hour == 13


def test_derive_file_type():
    assert derive_file_type("C:/x/report.PDF") == "pdf"
    assert derive_file_type("mail.msg") == "msg"
    assert derive_file_type(None) is None
    assert derive_file_type("noext") is None


def test_promote_record_maps_typed_fields_and_keeps_leftovers():
    record = {"Cust": "Smith, J", "Sent": "07/16/2026", "Widget": "keepme", "MD5": "abc123"}
    mapping = {"custodian": "Cust", "date_sent": "Sent", "file_hash_md5": "MD5"}
    typed, leftover = promote_record(record, mapping)
    assert typed["custodian"] == "Smith, J"
    assert typed["date_sent"].year == 2026
    assert typed["file_hash_md5"] == "abc123"
    # unmapped column preserved; mapped originals still kept in leftover metadata
    assert leftover["Widget"] == "keepme"
    assert leftover["Sent"] == "07/16/2026"   # original string retained (nothing lost)


def test_promote_record_ignores_structural_targets():
    # bates/text_link/native_link are structural — not returned as typed metadata
    typed, _ = promote_record({"BegBates": "ABC-1"}, {"bates_begin": "BegBates"})
    assert "bates_begin" not in typed
