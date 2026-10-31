"""Fake-session tests for POST /api/documents/bulk-tag-by-query."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.tags as rt
from app.schemas import BulkTagByQueryRequest, BulkTagQuery
from tests.fakes import FakeSession, FakeUser

A, B, C = (uuid4() for _ in range(3))


class FakeTag:
    def __init__(self, tag_id=5, category="responsiveness", name="Not Responsive",
                 is_privilege=False, production_id=None):
        self.id = tag_id
        self.category = category
        self.name = name
        self.is_privilege = is_privilege
        self.production_id = production_id


def _patch(monkeypatch, *, roles=None, accessible=(1, 2), matched=None, family=None,
           already=(), conflicts=None, conflict_names=None):
    """roles: production_id -> role (default reviewer everywhere)."""
    roles = roles or {}
    state = {"apply": None, "audit": [], "expanded": False}
    matched = matched if matched is not None else {A: 1, B: 1}

    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return roles.get(production_id, "reviewer")

    async def fake_log(db, user, action, resource_type, resource_id=None, **kwargs):
        state["audit"].append((action, kwargs))

    async def fake_resolve_query(db, query, acc):
        return dict(matched)

    async def fake_resolve_explicit(db, doc_ids):
        return dict(matched)

    async def fake_expand(db, docs):
        state["expanded"] = True
        return {**docs, **(family or {})}

    async def fake_already(db, tag_id, ids):
        return set(already)

    async def fake_conflicts(db, tag, ids):
        return dict(conflicts or {}), dict(conflict_names or {})

    async def fake_apply(db, plan, tag_id, user_id, override):
        state["apply"] = (plan, tag_id, user_id, override)
        return len(plan.coded) if override else 0

    monkeypatch.setattr(rt, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rt, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rt, "log_action", fake_log)
    monkeypatch.setattr(rt.bt, "resolve_query_docs", fake_resolve_query)
    monkeypatch.setattr(rt.bt, "resolve_explicit_docs", fake_resolve_explicit)
    monkeypatch.setattr(rt.bt, "expand_attachments", fake_expand)
    monkeypatch.setattr(rt.bt, "already_tagged_ids", fake_already)
    monkeypatch.setattr(rt.bt, "conflicting_tags", fake_conflicts)
    monkeypatch.setattr(rt.bt, "apply_plan", fake_apply)
    return state


def _run(body, tag=None):
    db = FakeSession(get_objects={("Tag", 5): tag} if tag else {})
    out = asyncio.run(rt.bulk_tag_by_query(body=body, db=db, user=FakeUser()))
    return out, db


def _query_body(**kw):
    return BulkTagByQueryRequest(tag_id=5, query=BulkTagQuery(email_address="jane@x.com"), **kw)


def _raises(body, tag, status):
    with pytest.raises(HTTPException) as exc:
        _run(body, tag)
    assert exc.value.status_code == status
    return exc.value


def test_unknown_tag_404(monkeypatch):
    _patch(monkeypatch)
    _raises(_query_body(), None, 404)


def test_override_on_non_privilege_tag_422(monkeypatch):
    _patch(monkeypatch, roles={1: "manager"})
    _raises(_query_body(disposition="withhold"), FakeTag(), 422)


def test_invalid_disposition_422(monkeypatch):
    _patch(monkeypatch, roles={1: "manager"})
    _raises(_query_body(disposition="burn"), FakeTag(is_privilege=True, category="privilege"), 422)


def test_query_without_criteria_422(monkeypatch):
    _patch(monkeypatch)

    async def raise_no_criteria(db, query, acc):
        raise rt.bt.NoCriteriaError

    monkeypatch.setattr(rt.bt, "resolve_query_docs", raise_no_criteria)
    _raises(_query_body(), FakeTag(), 422)


def test_dry_run_returns_counts_and_writes_nothing(monkeypatch):
    state = _patch(monkeypatch, family={C: 1}, already=(A,),
                   conflicts={B: {1}}, conflict_names={1: "Responsive"})
    out, db = _run(_query_body(dry_run=True), FakeTag())
    assert out == {
        "matched": 2, "attachments_added": 1, "total": 3, "already_tagged": 1,
        "conflicts": 1, "conflict_tags": [{"tag_id": 1, "name": "Responsive", "count": 1}],
    }
    assert state["apply"] is None
    assert db.commits == 0


def test_apply_with_unresolved_conflicts_409(monkeypatch):
    state = _patch(monkeypatch, conflicts={B: {1}}, conflict_names={1: "Responsive"})
    err = _raises(_query_body(), FakeTag(), 409)
    assert err.detail == "conflicts_unresolved"
    assert state["apply"] is None


def test_apply_switch_commits_and_audits(monkeypatch):
    state = _patch(monkeypatch, conflicts={B: {1}}, conflict_names={1: "Responsive"})
    out, db = _run(_query_body(conflict_action="switch"), FakeTag())
    assert out == {"total": 2, "tagged": 2, "already_tagged": 0, "switched": 1, "left": 0, "overridden": 0}
    assert db.commits == 1
    plan, tag_id, user_id, override = state["apply"]
    assert plan.switch == {B: {1}} and tag_id == 5 and user_id == "u1" and override == {}
    action, kwargs = state["audit"][0]
    assert action == "bulk_tag_by_query"
    assert kwargs["production_id"] == 1
    assert kwargs["details"]["conflict_action"] == "switch"
    assert kwargs["details"]["criteria"]["email_address"] == "jane@x.com"


def test_apply_leave_skips_conflicting(monkeypatch):
    _patch(monkeypatch, conflicts={B: {1}}, conflict_names={1: "Responsive"})
    out, _ = _run(_query_body(conflict_action="leave"), FakeTag())
    assert out["tagged"] == 1 and out["left"] == 1 and out["switched"] == 0


def test_readonly_403(monkeypatch):
    _patch(monkeypatch, roles={1: "readonly"})
    _raises(_query_body(), FakeTag(), 403)


def test_role_checked_for_every_production(monkeypatch):
    _patch(monkeypatch, matched={A: 1, B: 2}, roles={1: "reviewer", 2: "readonly"})
    _raises(_query_body(), FakeTag(), 403)


def test_override_requires_manager(monkeypatch):
    _patch(monkeypatch, roles={1: "reviewer"})
    priv = FakeTag(is_privilege=True, category="privilege", name="Attorney-Client")
    _raises(_query_body(disposition="withhold"), priv, 403)


def test_override_as_manager_passes_fields(monkeypatch):
    state = _patch(monkeypatch, roles={1: "manager"})
    priv = FakeTag(is_privilege=True, category="privilege", name="Attorney-Client")
    out, _ = _run(_query_body(disposition="withhold", description="  Counsel comms  "), priv)
    assert state["apply"][3] == {"privilege_disposition": "withhold",
                                 "privilege_description": "Counsel comms"}
    assert out["overridden"] == 2


def test_blank_description_is_not_an_override(monkeypatch):
    state = _patch(monkeypatch, roles={1: "reviewer"})
    priv = FakeTag(is_privilege=True, category="privilege")
    _run(_query_body(description="   "), priv)
    assert state["apply"][3] == {}


def test_tag_from_other_production_422(monkeypatch):
    _patch(monkeypatch, matched={A: 2})
    _raises(_query_body(), FakeTag(production_id=1), 422)


def test_explicit_ids_missing_404(monkeypatch):
    _patch(monkeypatch, matched={A: 1})
    _raises(BulkTagByQueryRequest(tag_id=5, doc_ids=[A, B]), FakeTag(), 404)


def test_explicit_ids_inaccessible_403(monkeypatch):
    _patch(monkeypatch, matched={A: 9}, accessible=(1,))
    _raises(BulkTagByQueryRequest(tag_id=5, doc_ids=[A]), FakeTag(), 403)


def test_attachments_off_skips_expansion(monkeypatch):
    state = _patch(monkeypatch, family={C: 1})
    out, _ = _run(_query_body(include_attachments=False, dry_run=True), FakeTag())
    assert state["expanded"] is False
    assert out["total"] == 2 and out["attachments_added"] == 0


def test_over_cap_422(monkeypatch):
    _patch(monkeypatch, family={C: 1})
    monkeypatch.setattr(rt.bt, "MAX_BULK_DOCS", 2)
    _raises(_query_body(dry_run=True), FakeTag(), 422)
