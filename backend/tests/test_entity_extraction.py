"""Pure-function tests for entity extraction: parsing, slicing, offsets, dates."""
import json
from datetime import date

import pytest

from app.services.entity_extraction import (
    build_extraction_prompt, locate_mentions, merge_parsed, parse_email_addresses,
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


_VALID_EVENT = {
    "description": "Board meeting", "type": "meeting",
    "date": "2020-01-01", "participants": ["Jorge Rivera"],
}

_MALFORMED_PAYLOADS = [
    {"entities": {"name": "X"}, "events": [], "relationships": []},
    {"entities": 5, "events": [], "relationships": []},
    {"entities": True, "events": [], "relationships": []},
    {"entities": [], "events": {"x": 1}, "relationships": []},
    {"entities": [{"name": "X", "type": "person", "emails": 5}], "events": [], "relationships": []},
    {"entities": [{"name": "X", "type": "person", "surface_forms": 5}], "events": [], "relationships": []},
    {"entities": [], "events": [{"description": "y", "type": "meeting", "participants": 5}], "relationships": []},
]


@pytest.mark.parametrize("payload", _MALFORMED_PAYLOADS)
def test_parse_never_raises_on_malformed_shapes(payload):
    out = parse_extraction_response(json.dumps(payload))
    assert isinstance(out, dict)
    assert set(out.keys()) == {"entities", "events", "relationships"}
    assert isinstance(out["entities"], list)
    assert isinstance(out["events"], list)
    assert isinstance(out["relationships"], list)


def test_parse_malformed_field_does_not_block_valid_sibling_field():
    payload = {"entities": 5, "events": [_VALID_EVENT], "relationships": []}
    out = parse_extraction_response(json.dumps(payload))
    assert out["entities"] == []
    assert len(out["events"]) == 1
    assert out["events"][0]["description"] == "Board meeting"


def test_parse_truncates_oversized_lists():
    entities = [
        {"name": f"Person {i}", "type": "person", "surface_forms": [f"Person {i}"]}
        for i in range(60)
    ]
    out = parse_extraction_response(json.dumps({"entities": entities, "events": [], "relationships": []}))
    assert len(out["entities"]) == 50

    one_entity = [{
        "name": "Jorge Rivera", "type": "person",
        "surface_forms": [f"Form {i}" for i in range(12)],
    }]
    out2 = parse_extraction_response(json.dumps({"entities": one_entity, "events": [], "relationships": []}))
    assert len(out2["entities"][0]["surface_forms"]) == 10


def test_merge_parsed_dedupes_events_and_relationships_across_overlapping_slices():
    # Slices overlap by 2000 chars (see slice_text), so the same event/edge
    # can be re-extracted verbatim from two adjacent slices; merge_parsed
    # must keep only one of each (regression for FINDING 2).
    slice1 = {
        "entities": [],
        "events": [{"description": "Board meeting", "type": "meeting", "date": "2019-03-15", "participants": []}],
        "relationships": [{"source": "Jorge Rivera", "target": "Acme Corp", "type": "employment", "evidence": "sig"}],
    }
    slice2 = {
        "entities": [],
        "events": [{"description": "Board meeting", "type": "meeting", "date": "2019-03-15", "participants": []}],
        "relationships": [{"source": "Jorge Rivera", "target": "Acme Corp", "type": "employment", "evidence": "sig"}],
    }
    merged = merge_parsed([slice1, slice2])
    assert len(merged["events"]) == 1
    assert len(merged["relationships"]) == 1


def test_slice_text_guards_degenerate_overlap():
    text = "x" * 1000

    slices = slice_text(text, window=100, overlap=100)
    assert 0 < len(slices) <= 1000
    assert all(len(s) <= 100 for s in slices)
    assert text.endswith(slices[-1])

    slices2 = slice_text(text, window=100, overlap=150)
    assert 0 < len(slices2) <= 1000
    assert all(len(s) <= 100 for s in slices2)
    assert text.endswith(slices2[-1])


def _entity_json(*names):
    return json.dumps({
        "entities": [
            {"name": n, "type": "org" if "Court" in n or "Reporting" in n else "person",
             "surface_forms": [n], "role": None, "emails": []}
            for n in names
        ],
        "events": [], "relationships": [],
    })


def test_parse_drops_litigation_process_noise():
    raw = _entity_json(
        "THE COURT", "Veritext Court Reporting", "Superior Court of Fulton County",
        "Court of Appeals", "Clerk of Court", "Plaintiff", "Defendants", "Notary Public",
        "Jorge Rivera",
    )
    out = parse_extraction_response(raw)
    assert [e["name"] for e in out["entities"]] == ["Jorge Rivera"]


def test_parse_keeps_real_actors_with_courtlike_substrings():
    # Named people and firms must never be caught by the noise patterns.
    raw = _entity_json("Courtney Smith", "Harcourt Industries", "Dana Wu")
    out = parse_extraction_response(raw)
    assert [e["name"] for e in out["entities"]] == ["Courtney Smith", "Harcourt Industries", "Dana Wu"]
