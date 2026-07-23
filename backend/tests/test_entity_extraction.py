"""Pure-function tests for entity extraction: parsing, slicing, offsets, dates."""
from datetime import date

from app.services.entity_extraction import (
    build_extraction_prompt, locate_mentions, parse_email_addresses,
    parse_event_date, parse_extraction_response, slice_text,
)


def test_parse_valid_response():
    raw = '''```json
{"entities": [{"name": "Jorge Rivera", "type": "person", "surface_forms": ["Jorge Rivera", "J. Rivera"], "role": "CFO", "emails": ["jr@acme.com"]}],
 "events": [{"description": "Board meeting", "type": "meeting", "date": "2019-03-15", "participants": ["Jorge Rivera"]}],
 "relationships": [{"source": "Jorge Rivera", "target": "Acme Corp", "type": "employment", "evidence": "signature block"}]}
```'''
    out = parse_extraction_response(raw)
    assert out["entities"][0]["name"] == "Jorge Rivera"
    assert out["entities"][0]["type"] == "person"
    assert out["events"][0]["type"] == "meeting"
    assert out["relationships"][0]["type"] == "employment"


def test_parse_garbage_returns_empty_sentinel():
    out = parse_extraction_response("I could not process this document.")
    assert out == {"entities": [], "events": [], "relationships": []}


def test_parse_drops_invalid_enum_values():
    raw = '{"entities": [{"name": "X", "type": "alien", "surface_forms": ["X"]}], "events": [{"description": "y", "type": "party", "participants": []}], "relationships": []}'
    out = parse_extraction_response(raw)
    assert out["entities"] == []          # bad entity type dropped
    assert out["events"][0]["type"] == "other"  # bad event type coerced


def test_locate_mentions_finds_all_occurrences_with_offsets():
    text = "Jorge Rivera met the board. Later, Rivera signed. Jorge Rivera left."
    mentions = locate_mentions(text, ["Jorge Rivera", "Rivera"])
    spans = {(m["start_offset"], m["end_offset"]) for m in mentions}
    assert (0, 12) in spans and (50, 62) in spans      # both "Jorge Rivera"
    assert (35, 41) in spans                            # bare "Rivera"
    # longest-form-first: bare "Rivera" inside "Jorge Rivera" is NOT double-counted
    assert (6, 12) not in spans and (56, 62) not in spans
    for m in mentions:
        assert text[m["start_offset"]:m["end_offset"]] == m["surface_text"]


def test_locate_mentions_missing_form_returns_nothing_for_it():
    assert locate_mentions("nothing here", ["Jorge Rivera"]) == []


def test_slice_text_short_is_single_slice():
    assert slice_text("abc") == ["abc"]


def test_slice_text_long_overlaps():
    text = "x" * 300_000
    slices = slice_text(text, window=140_000, overlap=2_000)
    assert len(slices) == 3
    assert all(len(s) <= 140_000 for s in slices)


def test_parse_event_date_precisions():
    assert parse_event_date("2019-03-15") == (date(2019, 3, 15), "day")
    assert parse_event_date("2019-03") == (date(2019, 3, 1), "month")
    assert parse_event_date("2019") == (date(2019, 1, 1), "year")
    assert parse_event_date(None) == (None, "unknown")
    assert parse_event_date("sometime") == (None, "unknown")


def test_parse_email_addresses():
    assert parse_email_addresses('Jorge Rivera <jr@acme.com>') == [("Jorge Rivera", "jr@acme.com")]
    assert parse_email_addresses('jr@acme.com; Ana Cruz <ana@firm.law>') == [
        ("", "jr@acme.com"), ("Ana Cruz", "ana@firm.law")]
    assert parse_email_addresses(None) == []


def test_prompt_includes_document_text():
    assert "the quick brown" in build_extraction_prompt("the quick brown")
