"""Fake-session tests for the production timeline endpoint."""

import asyncio
import uuid
from datetime import date

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity, OntologyEvent
from tests.fakes import FakeResult, FakeSession, FakeUser


def _event(eid, d=None, precision="unknown", etype="meeting", significance=None, date_source_text=None):
    ev = OntologyEvent(production_id=1, event_type=etype, description=f"Event {eid}",
                       event_date=d, date_precision=precision, document_id=uuid.uuid4(),
                       significance=significance, date_source_text=date_source_text)
    ev.id = eid
    return ev


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)


def test_timeline_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_production_timeline(production_id=1, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


def test_timeline_returns_events_with_participants_and_doc(monkeypatch):
    _patch(monkeypatch)
    ent = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                 canonical_name="Jorge Rivera", aliases=[], attributes={}, mention_count=5)
    ev = _event(10, d=date(2019, 3, 15), precision="day")
    db = FakeSession(responders=[
        ("count(ontology_events", FakeResult(scalar=1)),
        # page query returns (event, bates, title) rows
        ("FROM ontology_events", FakeResult(rows=[(ev, "ABC-0001", "Board deck")])),
        # participants query returns (event_id, entity) rows
        ("FROM event_participants", FakeResult(rows=[(10, ent)])),
    ])
    out = asyncio.run(er.get_production_timeline(production_id=1, db=db, user=FakeUser()))
    assert out.total == 1
    e = out.events[0]
    assert e.event_date == "2019-03-15" and e.date_precision == "day"
    assert e.bates_begin == "ABC-0001"
    assert e.participants[0].canonical_name == "Jorge Rivera"


def test_per_page_clamp_pure():
    """Test the _clamp_per_page function directly."""
    assert er._clamp_per_page(5000) == 100
    assert er._clamp_per_page(0) == 1
    assert er._clamp_per_page(50) == 50


def test_timeline_clamps_per_page(monkeypatch):
    _patch(monkeypatch)
    spy_calls = []
    def spy(v):
        spy_calls.append(v)
        return 100
    monkeypatch.setattr(er, "_clamp_per_page", spy)
    db = FakeSession(responders=[
        ("count(ontology_events", FakeResult(scalar=0)),
        ("FROM ontology_events", FakeResult(rows=[])),
        ("FROM event_participants", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_timeline(
        production_id=1, per_page=5000, db=db, user=FakeUser()))
    assert out.events == [] and out.total == 0
    assert spy_calls == [5000]


def test_timeline_null_date_serializes_none(monkeypatch):
    _patch(monkeypatch)
    ev = _event(11, d=None, precision="unknown")
    db = FakeSession(responders=[
        ("count(ontology_events", FakeResult(scalar=1)),
        ("FROM ontology_events", FakeResult(rows=[(ev, "ABC-0002", None)])),
        ("FROM event_participants", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_timeline(production_id=1, db=db, user=FakeUser()))
    assert out.events[0].event_date is None


def test_timeline_returns_significance_and_source(monkeypatch):
    _patch(monkeypatch)
    ev = _event(20, d=date(2021, 6, 1), precision="month", significance=5,
                date_source_text="filed on June 2021")
    db = FakeSession(responders=[
        ("count(ontology_events", FakeResult(scalar=1)),
        ("FROM ontology_events", FakeResult(rows=[(ev, "ABC-0100", "Complaint")])),
        ("FROM event_participants", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_timeline(production_id=1, db=db, user=FakeUser()))
    e = out.events[0]
    assert e.significance == 5
    assert e.date_source_text == "filed on June 2021"


def test_timeline_null_significance_defaults_to_three(monkeypatch):
    _patch(monkeypatch)
    ev = _event(21, significance=None, date_source_text=None)
    db = FakeSession(responders=[
        ("count(ontology_events", FakeResult(scalar=1)),
        ("FROM ontology_events", FakeResult(rows=[(ev, "ABC-0101", None)])),
        ("FROM event_participants", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_timeline(production_id=1, db=db, user=FakeUser()))
    assert out.events[0].significance == 3
    assert out.events[0].date_source_text is None


def test_timeline_min_significance_default_filters_via_coalesce(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[
        ("count(ontology_events", FakeResult(scalar=0)),
        ("FROM ontology_events", FakeResult(rows=[])),
        ("FROM event_participants", FakeResult(rows=[])),
    ])
    asyncio.run(er.get_production_timeline(production_id=1, db=db, user=FakeUser()))
    joined = "\n".join(db.executed)
    # Default min_significance=3 must emit a COALESCE(significance, 3) >= N guard
    # so null/legacy rows are treated as 3, not hidden. Assert the COMPILED SQL.
    assert "coalesce(ontology_events.significance" in joined
    assert ">=" in joined


def test_timeline_min_significance_one_includes_all(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[
        ("count(ontology_events", FakeResult(scalar=0)),
        ("FROM ontology_events", FakeResult(rows=[])),
        ("FROM event_participants", FakeResult(rows=[])),
    ])
    asyncio.run(er.get_production_timeline(
        production_id=1, min_significance=1, db=db, user=FakeUser()))
    joined = "\n".join(db.executed)
    # min_significance=1 returns everything — no significance guard in SQL.
    assert "coalesce(ontology_events.significance" not in joined


def test_undated_count_distinct(monkeypatch):
    _patch(monkeypatch)
    ev = _event(12, d=date(2020, 1, 10), precision="day")
    db = FakeSession(responders=[
        ("event_date IS NULL", FakeResult(scalar=2)),
        ("count(ontology_events", FakeResult(scalar=7)),
        ("FROM ontology_events", FakeResult(rows=[(ev, "ABC-0003", "Contract")])),
        ("FROM event_participants", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_timeline(production_id=1, db=db, user=FakeUser()))
    assert out.total == 7
    assert out.undated_count == 2
