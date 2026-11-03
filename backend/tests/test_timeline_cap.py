"""Timeline review keeps a deterministic subset of events above the cap."""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import app.services.timeline_review as tr
from app.services.timeline_review import MAX_TIMELINE_EVENTS, cap_events


def fake_event(i, mentions=None, day=None):
    return SimpleNamespace(
        id=i + 1,
        event_date=None if day is None else date(2020, 1, 1) + timedelta(days=day),
        date_precision="day", event_type="meeting", significance=3,
        description=f"ACME meeting {i}", date_source_text=None,
        mention_count=(i % 7) if mentions is None else mentions,
    )


def test_cap_value():
    assert MAX_TIMELINE_EVENTS == 3_000


def test_timeline_cap_is_deterministic():
    ev = [fake_event(i, day=i % 400 if i % 5 else None) for i in range(5_000)]
    a, b = cap_events(ev), cap_events(list(reversed(ev)))
    assert len(a) == 3_000 and [e.id for e in a] == [e.id for e in b]


def test_under_cap_returns_everything():
    ev = [fake_event(i) for i in range(10)]
    assert sorted(e.id for e in cap_events(ev)) == sorted(e.id for e in ev)


def test_cap_prefers_mentions_then_earliest_date():
    many = fake_event(0, mentions=9, day=300)
    early = fake_event(1, mentions=1, day=1)
    late = fake_event(2, mentions=1, day=200)
    undated = fake_event(3, mentions=1, day=None)
    filler = [fake_event(10 + i, mentions=0, day=0) for i in range(MAX_TIMELINE_EVENTS)]
    kept = {e.id for e in cap_events([undated, late, early, many, *filler])}
    assert {many.id, early.id, late.id, undated.id} <= kept
    # Ties at the lowest mention count are broken by earliest date, then id.
    dropped = {e.id for e in filler} - kept
    assert len(dropped) == 4
    assert dropped == {e.id for e in filler[-4:]}


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeDB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt, params=None):
        return _Rows(self._results.pop(0))

    async def commit(self):
        pass


def test_run_timeline_review_samples_above_cap(monkeypatch):
    events = [fake_event(i, day=i % 30) for i in range(MAX_TIMELINE_EVENTS + 5)]
    rows = [(e, f"ACME-{e.id:06d}") for e in events]
    # Event 1 has two participants, so it outranks everything else.
    names = [(1, "Alice Example"), (1, "Bob Example")]
    db = FakeDB([rows, names])
    seen = {}

    async def fake_call(user_content):
        seen["lines"] = user_content.count('{"id"')
        return '{"verdicts": []}', "end_turn", {"input_tokens": 1, "output_tokens": 1, "served_by": "m"}

    async def fake_apply(db, production_id, verdicts, actor):
        return {"merged": 0, "deleted": 0, "edited": 0, "rerated": 0,
                "skipped": 0, "skip_reasons": []}

    logged = []

    async def fake_log(db, actor, action, *a, **k):
        logged.append(k.get("details"))

    monkeypatch.setattr(tr, "_call_review_model", fake_call)
    monkeypatch.setattr(tr, "apply_verdicts", fake_apply)
    monkeypatch.setattr(tr, "log_action", fake_log)
    summary = asyncio.run(tr.run_timeline_review(db, 1, actor=object()))
    assert seen["lines"] == MAX_TIMELINE_EVENTS
    assert summary["sampled"] is True
    assert summary["events_total"] == MAX_TIMELINE_EVENTS + 5
    assert summary["events_reviewed"] == MAX_TIMELINE_EVENTS
    assert logged[-1]["sampled"] is True
