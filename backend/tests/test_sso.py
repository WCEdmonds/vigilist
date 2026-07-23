"""Fake-session tests for enterprise SSO (P4-1). No DB/Firebase."""

import asyncio

import pytest
from fastapi import HTTPException

import app.routers.auth as ra
from app.services.sso import enforce_org_sso, resolve_sso_org
from tests.fakes import FakeResult, FakeSession, FakeUser


class FakeOrg:
    def __init__(self, **kw):
        self.id = kw.get("org_id", 1)
        self.slug = kw.get("slug", "acme")
        self.name = kw.get("name", "Acme LLP")
        self.member_domains = kw.get("member_domains", ["acme.com"])
        self.creator_emails = kw.get("creator_emails", ["admin@acme.com"])
        self.sso_provider_id = kw.get("sso_provider_id", "saml.acme")
        self.sso_enforced = kw.get("sso_enforced", True)


def _db(orgs):
    return FakeSession(responders=[("organizations", FakeResult(items=orgs))])


# --- enforce_org_sso --------------------------------------------------------

def test_enforced_org_rejects_password_login():
    db = _db([FakeOrg()])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(enforce_org_sso(db, "jane@acme.com", "password"))
    assert exc.value.status_code == 403
    assert "single sign-on" in exc.value.detail


def test_enforced_org_accepts_matching_provider():
    db = _db([FakeOrg()])
    asyncio.run(enforce_org_sso(db, "jane@acme.com", "saml.acme"))  # no raise


def test_creator_email_exempt_from_enforcement():
    db = _db([FakeOrg()])
    asyncio.run(enforce_org_sso(db, "Admin@Acme.com", "password"))  # no raise


def test_unenforced_org_allows_any_provider():
    db = _db([FakeOrg(sso_enforced=False)])
    asyncio.run(enforce_org_sso(db, "jane@acme.com", "password"))


def test_enforced_without_provider_allows():
    db = _db([FakeOrg(sso_provider_id=None)])
    asyncio.run(enforce_org_sso(db, "jane@acme.com", "password"))


def test_foreign_domain_untouched():
    db = _db([FakeOrg()])
    asyncio.run(enforce_org_sso(db, "someone@other.com", "password"))


# --- resolve_sso_org --------------------------------------------------------

def test_resolve_by_slug():
    org = FakeOrg()
    db = FakeSession(responders=[("organizations", FakeResult(scalar=org))])
    out = asyncio.run(resolve_sso_org(db, "acme", None))
    assert out is org


def test_resolve_by_email_domain():
    org = FakeOrg()
    db = FakeSession(responders=[
        ("organizations", lambda sql: FakeResult(items=[org]) if "IN" not in sql else FakeResult()),
    ])
    out = asyncio.run(resolve_sso_org(db, None, "jane@acme.com"))
    assert out is org


def test_resolve_miss():
    db = FakeSession()
    assert asyncio.run(resolve_sso_org(db, None, "x@nowhere.com")) is None


# --- endpoints --------------------------------------------------------------

def test_sso_config_public_shape(monkeypatch):
    org = FakeOrg()

    async def fake_resolve(db, slug, email):
        return org

    monkeypatch.setattr(ra, "resolve_sso_org", fake_resolve)
    out = asyncio.run(ra.sso_config(slug="acme", email=None, db=FakeSession()))
    assert out == {"provider_id": "saml.acme", "enforced": True, "org_name": "Acme LLP"}


def test_sso_config_miss(monkeypatch):
    async def fake_resolve(db, slug, email):
        return None

    monkeypatch.setattr(ra, "resolve_sso_org", fake_resolve)
    out = asyncio.run(ra.sso_config(slug=None, email="x@nowhere.com", db=FakeSession()))
    assert out["provider_id"] is None


def _org_db(org):
    return FakeSession(responders=[("organizations", FakeResult(scalar=org))])


def _admin(email="admin@acme.com"):
    u = FakeUser()
    u.email = email
    return u


def test_update_sso_gated_to_creators(monkeypatch):
    async def fake_log(*a, **kw):
        pass

    monkeypatch.setattr(ra, "log_action", fake_log)
    org = FakeOrg(sso_provider_id=None, sso_enforced=False)
    out = asyncio.run(ra.update_org_sso(
        slug="acme", body={"provider_id": "saml.acme", "enforced": True},
        db=_org_db(org), user=_admin()))
    assert out == {"slug": "acme", "provider_id": "saml.acme", "enforced": True}
    assert org.sso_provider_id == "saml.acme"
    assert org.sso_enforced is True


def test_update_sso_rejects_non_creator(monkeypatch):
    org = FakeOrg()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ra.update_org_sso(
            slug="acme", body={"provider_id": "saml.acme"},
            db=_org_db(org), user=_admin("jane@acme.com")))
    assert exc.value.status_code == 403


def test_update_sso_validates_provider_shape(monkeypatch):
    async def fake_log(*a, **kw):
        pass

    monkeypatch.setattr(ra, "log_action", fake_log)
    org = FakeOrg()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ra.update_org_sso(
            slug="acme", body={"provider_id": "ldap.acme"},
            db=_org_db(org), user=_admin()))
    assert exc.value.status_code == 422


def test_update_sso_enforce_requires_provider(monkeypatch):
    async def fake_log(*a, **kw):
        pass

    monkeypatch.setattr(ra, "log_action", fake_log)
    org = FakeOrg()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ra.update_org_sso(
            slug="acme", body={"provider_id": None, "enforced": True},
            db=_org_db(org), user=_admin()))
    assert exc.value.status_code == 422
