"""Tagging in the viewer codes the whole family by default, so attachments
stay coded with their parent email (customer batch-coding request)."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.tags as rt
from app.schemas import ApplyTagsRequest
from tests.fakes import FakeResult, FakeSession, FakeUser

PARENT, ATT1, ATT2 = (uuid4() for _ in range(3))
RESPONSIVE = SimpleNamespace(id=5, category="responsiveness", name="Responsive",
                             is_privilege=False, production_id=None)


def _doc(doc_id=PARENT, family_id="ACME 000004 -0776"):
    return SimpleNamespace(id=doc_id, production_id=1, family_id=family_id)


def _patch(monkeypatch, *, family=(ATT1, ATT2), already=(), conflicts=None):
    state = {"plans": [], "audit": []}

    async def fake_accessible(db, user):
        return [1]

    async def fake_role(db, user, production_id):
        return "reviewer"

    async def fake_log(db, user, action, resource_type, resource_id=None, **kwargs):
        state["audit"].append((action, resource_id, kwargs.get("details")))

    async def fake_expand(db, docs):
        return {**docs, **{d: 1 for d in family}}

    async def fake_already(db, tag_id, ids):
        return set(already)

    async def fake_conflicts(db, tag, ids):
        return dict(conflicts or {}), {6: "Not Responsive"}

    async def fake_apply(db, plan, tag_id, user_id, override, override_ids=None):
        state["plans"].append((plan, tag_id))
        return 0

    monkeypatch.setattr(rt, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rt, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rt, "log_action", fake_log)
    monkeypatch.setattr(rt.bt, "expand_attachments", fake_expand)
    monkeypatch.setattr(rt.bt, "already_tagged_ids", fake_already)
    monkeypatch.setattr(rt.bt, "conflicting_tags", fake_conflicts)
    monkeypatch.setattr(rt.bt, "apply_plan", fake_apply)
    return state


def _session(doc, responders=()):
    return FakeSession(
        get_objects={("Document", doc.id): doc, ("Tag", 5): RESPONSIVE},
        responders=[*responders, ("FROM document_tags", FakeResult(items=[]))],
    )


def test_apply_codes_every_family_member(monkeypatch):
    state = _patch(monkeypatch)
    db = _session(_doc())
    asyncio.run(rt.apply_tags(PARENT, ApplyTagsRequest(tag_ids=[5], family=True), db=db, user=FakeUser()))
    plan, tag_id = state["plans"][0]
    assert tag_id == 5
    assert plan.to_tag == {PARENT, ATT1, ATT2}
    assert {a[1] for a in state["audit"]} == {str(PARENT), str(ATT1), str(ATT2)}
    assert all(a[0] == "tag_applied" and a[2]["family"] for a in state["audit"])
    assert db.commits == 1


def test_apply_switches_conflicting_responsiveness_on_members(monkeypatch):
    state = _patch(monkeypatch, conflicts={ATT1: {6}})
    asyncio.run(rt.apply_tags(PARENT, ApplyTagsRequest(tag_ids=[5], family=True),
                              db=_session(_doc()), user=FakeUser()))
    plan, _ = state["plans"][0]
    assert plan.switch == {ATT1: {6}}
    assert ATT1 in plan.to_tag
    switched = [a for a in state["audit"] if a[1] == str(ATT1)][0][2]
    assert switched["replaced_tag_ids"] == [6]


def test_apply_skips_members_already_tagged(monkeypatch):
    state = _patch(monkeypatch, already=(ATT2,))
    asyncio.run(rt.apply_tags(PARENT, ApplyTagsRequest(tag_ids=[5], family=True),
                              db=_session(_doc()), user=FakeUser()))
    plan, _ = state["plans"][0]
    assert plan.to_tag == {PARENT, ATT1}
    assert str(ATT2) not in {a[1] for a in state["audit"]}


def test_document_without_family_falls_back_to_single_tagging(monkeypatch):
    state = _patch(monkeypatch)
    db = _session(_doc(family_id=None))
    asyncio.run(rt.apply_tags(PARENT, ApplyTagsRequest(tag_ids=[5], family=True), db=db, user=FakeUser()))
    assert state["plans"] == []          # old per-document path
    assert len(db.added) == 1


def test_family_flag_defaults_off_for_other_callers():
    assert ApplyTagsRequest(tag_ids=[5]).family is False


def test_remove_untags_the_whole_family(monkeypatch):
    state = _patch(monkeypatch)
    db = _session(_doc(), responders=[
        ("SELECT document_tags.document_id", FakeResult(rows=[(PARENT,), (ATT1,)])),
    ])
    out = asyncio.run(rt.remove_tag(PARENT, 5, family=True, db=db, user=FakeUser()))
    assert out == {"ok": True, "removed": 2}
    assert any(sql.startswith("DELETE FROM document_tags") for sql in db.executed)
    assert {a[1] for a in state["audit"]} == {str(PARENT), str(ATT1)}
    assert all(a[0] == "tag_removed" for a in state["audit"])


def test_remove_404s_when_no_member_has_the_tag(monkeypatch):
    _patch(monkeypatch)
    db = _session(_doc(), responders=[("SELECT document_tags.document_id", FakeResult(rows=[]))])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.remove_tag(PARENT, 5, family=True, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_family_endpoint_stays_inside_the_documents_production(monkeypatch):
    import app.routers.intelligence as ri

    async def fake_accessible(db, user):
        return [1, 2]

    monkeypatch.setattr(ri, "get_accessible_production_ids", fake_accessible)
    doc = SimpleNamespace(id=PARENT, production_id=1, family_id="ACME 000004 -0776", thread_id=None)
    db = FakeSession(get_objects={("Document", PARENT): doc})
    asyncio.run(ri.get_document_family(PARENT, db=db, user=FakeUser()))
    assert "documents.production_id = " in db.executed[0]
