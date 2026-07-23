"""Fake-session tests for tenant provisioning (P4)."""

import asyncio

import pytest

from app.services.provisioning import ProvisioningError, provision_tenant
from tests.fakes import FakeResult, FakeSession


def _db(existing_org=None, existing_user=None):
    """Responder order matters: the org-slug lookup and system-user lookup
    are distinguished by table name in the compiled SQL."""
    return FakeSession(responders=[
        ("FROM organizations", FakeResult(scalar=existing_org)),
        ("FROM users", FakeResult(scalar=existing_user)),
    ])


def _run(db, **kw):
    defaults = dict(slug="acme", name="Acme LLP", member_domains=["acme.com"])
    defaults.update(kw)
    return asyncio.run(provision_tenant(db, **defaults))


def test_provisions_and_audits():
    db = _db()
    org = _run(db, creator_emails=["MP@Acme.com"],
               sso_provider_id="saml.acme", sso_enforced=True)
    assert org.slug == "acme"
    assert org.member_domains == ["acme.com"]
    assert org.creator_emails == ["mp@acme.com"]      # lowercased
    assert org.sso_enforced is True
    # org + system user + audit row all persisted
    types = [type(o).__name__ for o in db.added]
    assert types == ["Organization", "User", "AuditLog"]
    audit = db.added[-1]
    assert audit.action == "org_provisioned"
    assert audit.details["slug"] == "acme"


def test_duplicate_slug_rejected():
    db = _db(existing_org=object())
    with pytest.raises(ProvisioningError, match="already exists"):
        _run(db)


def test_reserved_slug_rejected():
    with pytest.raises(ProvisioningError, match="reserved"):
        _run(_db(), slug="app")


def test_bad_slug_rejected():
    with pytest.raises(ProvisioningError, match="slug must"):
        _run(_db(), slug="Bad Slug!")


def test_bad_domain_rejected():
    with pytest.raises(ProvisioningError, match="invalid email domain"):
        _run(_db(), member_domains=["not-a-domain"])


def test_domain_normalization():
    org = _run(_db(), member_domains=[" @Acme.COM ", "acme.com"])
    assert org.member_domains == ["acme.com"]          # cleaned + deduped


def test_bad_role_rejected():
    with pytest.raises(ProvisioningError, match="member_role"):
        _run(_db(), member_role="czar")


def test_enforce_requires_provider_and_admin():
    with pytest.raises(ProvisioningError, match="without a provider"):
        _run(_db(), sso_enforced=True)
    with pytest.raises(ProvisioningError, match="escape hatch"):
        _run(_db(), sso_provider_id="saml.acme", sso_enforced=True)


def test_bad_provider_shape_rejected():
    with pytest.raises(ProvisioningError, match="sso_provider_id"):
        _run(_db(), sso_provider_id="ldap.acme")
