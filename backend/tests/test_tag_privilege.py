"""Fake-session tests for the tag privilege flag endpoint (P1-4/5)."""

import asyncio

import pytest
from fastapi import HTTPException

import app.routers.tags as rt
from app.schemas import TagPrivilegeUpdate
from tests.fakes import FakeSession, FakeUser


class FakeTag:
    def __init__(self, tag_id=7, production_id=1, is_privilege=False):
        self.id = tag_id
        self.name = "Attorney-Client"
        self.category = "privilege"
        self.color = "red"
        self.keyboard_shortcut = None
        self.production_id = production_id
        self.is_privilege = is_privilege


def _patch(monkeypatch, role="manager", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    audit_calls = []

    async def fake_log(db, user, action, resource_type, resource_id=None, **kwargs):
        audit_calls.append((action, resource_type, resource_id, kwargs))

    monkeypatch.setattr(rt, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rt, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rt, "log_action", fake_log)
    return audit_calls


def test_set_privilege_flag_as_manager(monkeypatch):
    audit_calls = _patch(monkeypatch, role="manager")
    tag = FakeTag()
    db = FakeSession(get_objects={("Tag", 7): tag})
    out = asyncio.run(rt.update_tag_privilege(
        tag_id=7, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert out.is_privilege is True
    assert tag.is_privilege is True
    assert audit_calls == [("tag_privilege_set", "tag", "7",
                            {"production_id": 1, "details": {"is_privilege": True}})]


def test_set_privilege_flag_blocked_for_reviewer(monkeypatch):
    audit_calls = _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("Tag", 7): FakeTag()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.update_tag_privilege(
            tag_id=7, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert exc.value.status_code == 403
    assert audit_calls == []


def test_set_privilege_flag_unknown_tag_404(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.update_tag_privilege(
            tag_id=99, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_global_tag_requires_manager_somewhere(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("Tag", 7): FakeTag(production_id=None)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.update_tag_privilege(
            tag_id=7, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_global_tag_allowed_for_manager(monkeypatch):
    _patch(monkeypatch, role="manager")
    tag = FakeTag(production_id=None)
    db = FakeSession(get_objects={("Tag", 7): tag})
    out = asyncio.run(rt.update_tag_privilege(
        tag_id=7, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert out.is_privilege is True
