"""Fake-session tests for the relationship-graph endpoint."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity
from tests.fakes import FakeResult, FakeSession, FakeUser

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _ent(eid, name, count=10):
    return Entity(id=eid, production_id=1, entity_type="person",
                  canonical_name=name, aliases=[], attributes={}, mention_count=count)


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)


def test_graph_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_production_graph(production_id=1, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


def test_graph_nodes_edges_and_cooccurrence_dedup(monkeypatch):
    _patch(monkeypatch)
    nodes = [_ent(A, "Jorge Rivera", 30), _ent(B, "Acme Corp", 20), _ent(C, "Ana Cruz", 10)]
    db = FakeSession(responders=[
        ("AS count_1", FakeResult(scalar=3)),
        ("FROM entities", FakeResult(items=nodes)),
        # stated edges: (source, target, relationship_type, weight)
        ("target_entity_id", FakeResult(rows=[(A, B, "employment", 2)])),
        # co-occurrence pairs: (a, b, shared) — includes the A-B pair which must be deduped
        ("em_a", FakeResult(rows=[(A, B, 5), (A, C, 3)])),
    ])
    out = asyncio.run(er.get_production_graph(production_id=1, db=db, user=FakeUser()))
    assert {n.id for n in out.nodes} == {A, B, C}
    kinds = {(e.source, e.target): e.kind for e in out.edges}
    assert kinds[(A, B)] == "stated"          # stated wins; no duplicate cooccurrence edge
    assert kinds[(A, C)] == "cooccurrence"
    assert out.truncated is False


def test_graph_truncation_flag(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[
        ("AS count_1", FakeResult(scalar=500)),
        ("FROM entities", FakeResult(items=[_ent(A, "X", 1)])),
        ("target_entity_id", FakeResult(rows=[])),
        ("em_a", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_graph(production_id=1, max_nodes=1, db=db, user=FakeUser()))
    assert out.truncated is True


def test_graph_clamps_params(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[
        ("AS count_1", FakeResult(scalar=200)),
        ("FROM entities", FakeResult(items=[])),
        ("target_entity_id", FakeResult(rows=[])),
        ("em_a", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_graph(
        production_id=1, max_nodes=10_000, min_shared_docs=0, db=db, user=FakeUser()))
    assert out.nodes == [] and out.edges == []
    assert out.truncated is True  # 200 > clamped max_nodes of 150
