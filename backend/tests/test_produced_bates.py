"""Tests for produced-Bates resolution (P2-5). No DB."""

import asyncio
from uuid import uuid4

from app.services.produced_bates import resolve_produced_bates, split_bates
from tests.fakes import FakeResult, FakeSession


class FakePS:
    def __init__(self, set_id=1, prefix="SMITH", padding=6):
        self.id = set_id
        self.prefix = prefix
        self.padding = padding


def test_split_bates_normalizes():
    assert split_bates("SMITH000123") == ("SMITH", 123)
    assert split_bates("smith-000123") == ("SMITH", 123)
    assert split_bates("SMITH 000123") == ("SMITH", 123)
    assert split_bates("no digits") is None
    assert split_bates("12345") is None
    assert split_bates("") is None


def test_resolve_finds_containing_range():
    doc_id = uuid4()
    db = FakeSession(responders=[
        ("FROM production_sets", FakeResult(items=[FakePS()])),
        ("production_set_items", FakeResult(scalar=doc_id)),
    ])
    out = asyncio.run(resolve_produced_bates(db, [1], None, "SMITH-000123"))
    assert out == doc_id


def test_resolve_none_when_no_matching_set():
    db = FakeSession()  # no locked sets
    assert asyncio.run(resolve_produced_bates(db, [1], None, "SMITH000123")) is None


def test_resolve_none_for_garbage():
    assert asyncio.run(resolve_produced_bates(FakeSession(), [1], None, "hello")) is None


def test_resolve_respects_empty_scope():
    assert asyncio.run(resolve_produced_bates(FakeSession(), [], None, "SMITH000001")) is None


def test_by_bates_falls_back_to_produced(monkeypatch):
    import app.routers.documents as rd

    doc_id = uuid4()

    async def fake_accessible(db, user):
        return [1]

    async def fake_resolve(db, accessible, production_id, bates):
        return doc_id

    async def fake_detail(doc, db):
        return {"id": str(doc.id)}

    class FakeDoc:
        def __init__(self):
            self.id = doc_id
            self.production_id = 1

    monkeypatch.setattr(rd, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rd, "resolve_produced_bates", fake_resolve)
    monkeypatch.setattr(rd, "_doc_detail", fake_detail)

    from tests.fakes import FakeUser

    db = FakeSession(responders=[
        ("documents.id =", FakeResult(items=[FakeDoc()])),
    ])
    out = asyncio.run(rd.get_by_bates(bates="SMITH000123", production_id=None,
                                      db=db, user=FakeUser()))
    assert out == {"id": str(doc_id)}
