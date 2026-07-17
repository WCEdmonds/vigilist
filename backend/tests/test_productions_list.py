"""Unit test for list_productions document_count enrichment.

Uses a fake session (no database) in the same spirit as test_org_access.py.
get_accessible_production_ids is monkeypatched so the fake session only has
to answer the two queries list_productions itself issues: the Production
select and the per-production document count.
"""

import asyncio
from datetime import datetime, timezone

import app.routers.productions as productions_router


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"


class FakeProduction:
    def __init__(self, pid, name, owner_id):
        self.id = pid
        self.name = name
        self.description = None
        self.owner_id = owner_id
        self.created_at = datetime(2026, 7, 1, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _query):
        return FakeResult(self._results.pop(0))


def test_list_productions_includes_document_count(monkeypatch):
    async def fake_ids(db, user):
        return [1, 2]

    monkeypatch.setattr(
        productions_router, "get_accessible_production_ids", fake_ids
    )

    prods = [FakeProduction(1, "Acme v. Barrett", "u1"), FakeProduction(2, "Smith", "u2")]
    # Second query result: (production_id, count) tuples — prod 2 has no docs.
    db = FakeSession([prods, [(1, 4218)]])

    out = asyncio.run(
        productions_router.list_productions(db=db, user=FakeUser("u1"))
    )

    assert [p.document_count for p in out] == [4218, 0]
    assert out[0].is_owner is True
    assert out[1].is_owner is False
