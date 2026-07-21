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


def test_normalize_date_non_str_returns_none():
    # Fix 1: non-str input must not raise AttributeError, must return None
    assert normalize_date(None) is None


def test_normalize_date_offset_utc_conversion():
    # Fix 3: offset -> UTC shift: 09:45 -04:00 == 13:45 UTC
    d = normalize_date("2026-07-16T09:45:00-04:00")
    assert d is not None
    assert d.astimezone(timezone.utc).hour == 13


def test_normalize_date_naive_treated_as_utc():
    # Fix 3: naive datetime from load-file is treated as UTC (hour unchanged)
    d = normalize_date("2026-07-16 09:45:00")
    assert d is not None
    assert d.hour == 9
    assert d.tzinfo is not None


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


def test_promote_record_preserves_falsy_data_in_leftover():
    # Fix 2: 0 and False are legitimate data values and must survive into leftover;
    # empty string must be dropped.
    record = {"count": 0, "enabled": False, "empty": "", "label": "hello"}
    mapping = {}
    _, leftover = promote_record(record, mapping)
    assert leftover["count"] == 0
    assert leftover["enabled"] is False
    assert "empty" not in leftover
    assert leftover["label"] == "hello"


def test_promote_record_ignores_structural_targets():
    # bates/text_link/native_link are structural — not returned as typed metadata
    # Fix 4: structural source column must also be absent from leftover
    typed, leftover = promote_record({"BegBates": "ABC-1"}, {"bates_begin": "BegBates"})
    assert "bates_begin" not in typed
    assert "BegBates" not in leftover


from app.services.metadata_normalize import normalize_bool


def test_normalize_bool():
    for v in ("Yes", "y", "TRUE", "t", "1"):
        assert normalize_bool(v) is True
    for v in ("No", "n", "false", "F", "0"):
        assert normalize_bool(v) is False
    for v in ("", "maybe", "  "):
        assert normalize_bool(v) is None
    assert normalize_bool(None) is None


def test_promote_record_family_thread_inclusive():
    record = {"Group Identifier": "FAM-1", "Thread ID": "TH-9", "Inclusive Email": "Yes"}
    mapping = {"family_id": "Group Identifier", "thread_id": "Thread ID", "is_inclusive": "Inclusive Email"}
    from app.services.metadata_normalize import promote_record
    typed, _ = promote_record(record, mapping)
    assert typed["family_id"] == "FAM-1"
    assert typed["thread_id"] == "TH-9"
    assert typed["is_inclusive"] is True
