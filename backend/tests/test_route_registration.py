"""Integration-level routing test: guards against cross-router path shadowing.

Regression for FINDING C1: entities.py used to register GET
/api/documents/entities-summary, but documents.py owns the whole
/api/documents/{doc_id} namespace and its router is included first in
main.py. Starlette matches routes across the WHOLE app in registration
order, so /api/documents/entities-summary always matched
documents.get_document(doc_id="entities-summary") and never reached the
chips handler. The fix moved the route to /api/entities-summary (a
namespace no other router owns). These tests exercise the real FastAPI
app end-to-end (TestClient), not just the handler function in isolation,
because the bug was in ROUTE MATCHING, which unit-testing the handler
directly can never catch.
"""

import uuid

from fastapi.testclient import TestClient

import app.routers.entities as er
from app.database import get_db
from app.main import app
from app.models import Entity
from app.routers.auth import get_current_user
from tests.fakes import FakeResult, FakeSession, FakeUser

client = TestClient(app)


def test_entities_summary_route_exists_and_is_auth_gated():
    """Unauthenticated GET /api/entities-summary must be rejected by the auth
    dependency (401/403), proving the route MATCHED a registered handler.
    If the route were still shadowed/unregistered at this path, Starlette
    would return 404 (no match) instead -- 404 is what this test guards
    against. It must also not be 422, which is what the OLD shadowed path
    produces once authenticated (see test below) -- a 422 here would mean
    some other UUID-typed path param is capturing this segment again.
    """
    resp = client.get("/api/entities-summary", params={"ids": ""})
    assert resp.status_code in (401, 403)


def test_documents_entities_summary_old_path_is_get_document_not_chips(monkeypatch):
    """Authenticated GET on the OLD shadowed path (/api/documents/entities-summary)
    must resolve to documents.get_document with doc_id="entities-summary", which
    fails UUID validation -> 422. This documents/pins the shadowing behavior the
    fix moved away from; it is not itself a bug, it's the trap the fix avoids.
    """
    app.dependency_overrides[get_current_user] = lambda: FakeUser()
    app.dependency_overrides[get_db] = lambda: FakeSession()
    try:
        resp = client.get("/api/documents/entities-summary")
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_entities_summary_new_path_reaches_chips_handler(monkeypatch):
    """Authenticated GET /api/entities-summary must reach
    entities.get_entities_summary and return the chips shape, NOT the 422
    that the old shadowed path produces for the exact same query string.
    """
    doc_id = uuid.uuid4()
    entity = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                     canonical_name="Jane Doe", aliases=[], attributes={}, mention_count=1)

    async def fake_accessible(db, user):
        return [1]

    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)

    fake_db = FakeSession(responders=[
        ("FROM entity_mentions", FakeResult(rows=[(doc_id, entity, 5)])),
    ])
    app.dependency_overrides[get_current_user] = lambda: FakeUser()
    app.dependency_overrides[get_db] = lambda: fake_db
    try:
        resp = client.get("/api/entities-summary", params={"ids": str(doc_id)})
        assert resp.status_code == 200
        body = resp.json()
        assert str(doc_id) in body["summaries"]
        assert body["summaries"][str(doc_id)][0]["canonical_name"] == "Jane Doe"
    finally:
        app.dependency_overrides.clear()
