"""Fake-session tests for the timeline AI review service."""

import json
import uuid
from datetime import date

import pytest

from app.models import OntologyEvent
from app.services import timeline_review as tr


def _event(eid, d=None, precision="unknown", etype="meeting", desc=None,
           quote=None, significance=3):
    ev = OntologyEvent(production_id=1, event_type=etype,
                       description=desc or f"Event {eid}",
                       event_date=d, date_precision=precision,
                       document_id=uuid.uuid4(), significance=significance,
                       date_source_text=quote)
    ev.id = eid
    return ev


def test_serialize_orders_chronologically_undated_last():
    evs = [_event(3), _event(1, d=date(2020, 5, 1), precision="day"),
           _event(2, d=date(2019, 1, 1), precision="year")]
    out = tr.serialize_timeline(evs, bates={}, participants={})
    ids = [json.loads(line)["id"] for line in out.splitlines()]
    assert ids == [2, 1, 3]


def test_serialize_line_shape():
    ev = _event(7, d=date(2020, 5, 1), precision="day", etype="payment",
                desc="Wire sent", quote="wired on May 1, 2020")
    out = tr.serialize_timeline([ev], bates={7: "ABC-0042"},
                                participants={7: ["Jorge Rivera"]})
    row = json.loads(out)
    assert row == {"id": 7, "date": "2020-05-01", "precision": "day",
                   "type": "payment", "desc": "Wire sent",
                   "quote": "wired on May 1, 2020", "bates": "ABC-0042",
                   "who": ["Jorge Rivera"]}


def test_user_content_mentions_count_and_embeds_events():
    ev = _event(1, d=date(2020, 1, 2), precision="day")
    serialized = tr.serialize_timeline([ev], {}, {})
    content = tr.build_review_user_content(serialized, 1)
    assert serialized in content
    assert "1 event" in content


def test_parse_valid_response():
    raw = json.dumps({"verdicts": [
        {"kind": "delete", "event_id": 4, "event_ids": None, "keep_id": None,
         "date": None, "precision": None, "event_type": None,
         "description": None, "reason": "court reporter logistics",
         "confidence": 0.95},
    ]})
    verdicts = tr.parse_review_response(raw)
    assert len(verdicts) == 1 and verdicts[0]["kind"] == "delete"


def test_parse_rejects_malformed_json():
    with pytest.raises(tr.ReviewError):
        tr.parse_review_response("{not json")


def test_parse_rejects_missing_verdicts_key():
    with pytest.raises(tr.ReviewError):
        tr.parse_review_response(json.dumps({"nope": []}))


def test_schema_is_strict():
    # Structured-output constraints: every object closed, everything required,
    # no numeric range constraints (unsupported by the API).
    s = json.dumps(tr.REVIEW_SCHEMA)
    assert "minimum" not in s and "maximum" not in s
    item = tr.REVIEW_SCHEMA["properties"]["verdicts"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"].keys())
