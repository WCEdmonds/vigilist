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


import asyncio

from app.models import AuditLog, EventParticipant
from tests.fakes import FakeResult, FakeSession, FakeUser


def _dbase(events, participant_rows=(), edited_ids=()):
    """FakeSession serving the three loads apply_verdicts performs."""
    return FakeSession(responders=[
        ("FROM audit_logs", FakeResult(rows=[(str(i),) for i in edited_ids])),
        ("FROM event_participants", FakeResult(rows=list(participant_rows))),
        ("FROM ontology_events", FakeResult(items=list(events))),
    ])


def _verdict(**kw):
    base = {"kind": None, "event_id": None, "event_ids": None, "keep_id": None,
            "date": None, "precision": None, "event_type": None,
            "description": None, "reason": "r", "confidence": 0.95}
    base.update(kw)
    return base


def _run(db, verdicts):
    return asyncio.run(tr.apply_verdicts(db, 1, verdicts, FakeUser()))


def test_delete_applies_with_snapshot_audit():
    ev = _event(4)
    db = _dbase([ev])
    summary = _run(db, [_verdict(kind="delete", event_id=4)])
    assert summary["deleted"] == 1 and db.deleted == [ev]
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert audits[0].action == "event_deleted_by_review"
    assert audits[0].details["snapshot"]["description"] == "Event 4"
    assert audits[0].details["actor"] == "ai_timeline_review"


def test_confidence_gate_skips():
    ev = _event(4)
    db = _dbase([ev])
    summary = _run(db, [_verdict(kind="delete", event_id=4, confidence=0.5)])
    assert summary["deleted"] == 0 and summary["skipped"] == 1
    assert db.deleted == []


def test_human_edited_event_never_deleted():
    ev = _event(4)
    db = _dbase([ev], edited_ids=[4])
    summary = _run(db, [_verdict(kind="delete", event_id=4)])
    assert summary["deleted"] == 0 and summary["skipped"] == 1 and db.deleted == []


def test_merge_unions_participants_and_deletes_absorbed():
    keeper, dupe = _event(1, d=date(2020, 1, 2), precision="day"), _event(2)
    ent_a, ent_b = uuid.uuid4(), uuid.uuid4()
    # keeper already has ent_a; dupe has ent_a (overlap) and ent_b (new)
    db = _dbase([keeper, dupe],
                participant_rows=[(1, ent_a), (2, ent_a), (2, ent_b)])
    summary = _run(db, [_verdict(kind="merge", event_ids=[1, 2], keep_id=1,
                                 description="Board approved the wire")])
    assert summary["merged"] == 1
    assert db.deleted == [dupe]
    new_parts = [p for p in db.added if isinstance(p, EventParticipant)]
    assert [(p.event_id, p.entity_id) for p in new_parts] == [(1, ent_b)]
    assert keeper.description == "Board approved the wire"
    actions = [a.action for a in db.added if isinstance(a, AuditLog)]
    assert "event_merged_by_review" in actions and "event_deleted_by_review" in actions


def test_merge_skips_keeper_corrections_when_keeper_human_edited():
    keeper, dupe = _event(1, desc="Human wording"), _event(2)
    db = _dbase([keeper, dupe], edited_ids=[1])
    summary = _run(db, [_verdict(kind="merge", event_ids=[1, 2], keep_id=1,
                                 description="AI wording")])
    assert summary["merged"] == 1 and db.deleted == [dupe]
    assert keeper.description == "Human wording"  # correction skipped


def test_merge_rejected_when_absorbed_event_human_edited():
    keeper, dupe = _event(1), _event(2)
    db = _dbase([keeper, dupe], edited_ids=[2])
    summary = _run(db, [_verdict(kind="merge", event_ids=[1, 2], keep_id=1)])
    assert summary["merged"] == 0 and summary["skipped"] == 1 and db.deleted == []


def test_edit_applies_date_and_type():
    ev = _event(5, d=date(2020, 1, 1), precision="year", etype="meeting")
    db = _dbase([ev])
    summary = _run(db, [_verdict(kind="edit", event_id=5, date="2020-03-15",
                                 precision="day", event_type="payment")])
    assert summary["edited"] == 1
    assert ev.event_date == date(2020, 3, 15) and ev.date_precision == "day"
    assert ev.event_type == "payment"
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert audits[0].action == "event_edited_by_review"
    assert audits[0].details["before"]["date_precision"] == "year"


def test_edit_rejects_invalid_event_type():
    ev = _event(5)
    db = _dbase([ev])
    summary = _run(db, [_verdict(kind="edit", event_id=5, event_type="nonsense")])
    assert summary["edited"] == 0 and summary["skipped"] == 1


def test_unknown_and_foreign_ids_skip_not_crash():
    ev = _event(1)
    db = _dbase([ev])
    summary = _run(db, [
        _verdict(kind="delete", event_id=999),
        _verdict(kind="merge", event_ids=[1, 999], keep_id=1),
        _verdict(kind="merge", event_ids=[1], keep_id=1),        # group too small
        _verdict(kind="merge", event_ids=[1, 999], keep_id=999),  # keeper unknown
        _verdict(kind="edit", event_id=1, date="not-a-date", precision="day"),
        _verdict(kind="frobnicate", event_id=1),
    ])
    assert summary["skipped"] == 6
    assert summary["merged"] == summary["deleted"] == summary["edited"] == 0


def test_merge_with_duplicate_ids_applies_once_no_crash():
    # The schema can't enforce event_ids uniqueness; a duplicated absorbed
    # id must not crash the batch (KeyError) — it merges once, cleanly.
    keeper, dupe = _event(1), _event(2)
    db = _dbase([keeper, dupe])
    summary = _run(db, [_verdict(kind="merge", event_ids=[1, 2, 2], keep_id=1)])
    assert summary["merged"] == 1 and db.deleted == [dupe]
