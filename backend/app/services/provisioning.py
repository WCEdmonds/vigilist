"""Tenant provisioning (P4): create/configure an Organization, audited.

Called from scripts/provision_tenant.py (ops CLI). Kept as a service so the
logic is unit-tested and an admin API can wrap it later without rewriting.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import ROLE_RANK
from app.models import AuditLog, Organization, User
from app.services.sso import PROVIDER_ID_RE

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Subdomains that must never become tenants.
RESERVED_SLUGS = {"app", "www", "api", "admin", "mail", "staging", "status", "docs"}

SYSTEM_USER_ID = "system-provisioning"
SYSTEM_USER_EMAIL = "provisioning@vigilist.co"


class ProvisioningError(ValueError):
    pass


def _clean_domains(domains: list[str]) -> list[str]:
    out = []
    for d in domains:
        d = (d or "").strip().lower().lstrip("@")
        if not d or "." not in d or "@" in d:
            raise ProvisioningError(f"invalid email domain: {d!r}")
        if d not in out:
            out.append(d)
    return out


def _clean_emails(emails: list[str]) -> list[str]:
    out = []
    for e in emails:
        e = (e or "").strip().lower()
        if "@" not in e:
            raise ProvisioningError(f"invalid email: {e!r}")
        if e not in out:
            out.append(e)
    return out


async def _system_user(db: AsyncSession) -> User:
    user = (await db.execute(
        select(User).where(User.id == SYSTEM_USER_ID)
    )).scalar_one_or_none()
    if user is None:
        user = User(id=SYSTEM_USER_ID, email=SYSTEM_USER_EMAIL,
                    display_name="Provisioning")
        db.add(user)
        await db.flush()
    return user


async def provision_tenant(
    db: AsyncSession,
    slug: str,
    name: str,
    member_domains: list[str],
    member_role: str = "reviewer",
    creator_emails: list[str] | None = None,
    sso_provider_id: str | None = None,
    sso_enforced: bool = False,
) -> Organization:
    """Create an Organization; audited under the system provisioning user.

    Raises ProvisioningError on invalid input or a duplicate slug — the CLI
    surfaces it as a friendly message, an API wrapper as a 422.
    """
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise ProvisioningError(
            "slug must be 1-63 chars of lowercase letters, digits, hyphens")
    if slug in RESERVED_SLUGS:
        raise ProvisioningError(f"slug {slug!r} is reserved")
    if not (name or "").strip():
        raise ProvisioningError("name is required")
    if member_role not in ROLE_RANK:
        raise ProvisioningError(f"member_role must be one of {sorted(ROLE_RANK)}")
    domains = _clean_domains(member_domains or [])
    if not domains:
        raise ProvisioningError("at least one member email domain is required")
    admins = _clean_emails(creator_emails or [])
    if sso_provider_id is not None and not PROVIDER_ID_RE.match(sso_provider_id):
        raise ProvisioningError("sso_provider_id must look like 'saml.name' or 'oidc.name'")
    if sso_enforced and not sso_provider_id:
        raise ProvisioningError("cannot enforce SSO without a provider id")
    if sso_enforced and not admins:
        raise ProvisioningError(
            "enforced SSO requires at least one creator email (the lockout escape hatch)")

    existing = (await db.execute(
        select(Organization).where(Organization.slug == slug)
    )).scalar_one_or_none()
    if existing is not None:
        raise ProvisioningError(f"organization slug {slug!r} already exists")

    org = Organization(
        slug=slug, name=name.strip(), member_role=member_role,
        member_domains=domains, creator_emails=admins,
        sso_provider_id=sso_provider_id, sso_enforced=sso_enforced,
    )
    db.add(org)
    await db.flush()

    actor = await _system_user(db)
    db.add(AuditLog(
        user_id=actor.id, user_email=actor.email,
        action="org_provisioned", resource_type="organization",
        resource_id=str(org.id),
        details={"slug": slug, "name": org.name, "member_domains": domains,
                 "member_role": member_role, "creator_emails": admins,
                 "sso_provider_id": sso_provider_id, "sso_enforced": sso_enforced},
    ))
    await db.commit()
    return org
