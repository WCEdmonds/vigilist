"""Per-organization SSO enforcement (P4-1).

An organization with sso_enforced requires its member-domain users to
authenticate through its bound Identity Platform provider. Enforcement is
server-side on every request; client-side hiding is UX only.
"""

from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization

PROVIDER_ID_RE = re.compile(r"^(saml|oidc)\.[A-Za-z0-9_-]+$")


def _domain(email: str) -> str:
    if "@" not in (email or ""):
        return ""
    return email.rsplit("@", 1)[1].strip().lower()


async def resolve_sso_org(db: AsyncSession, slug: str | None,
                          email: str | None) -> Organization | None:
    """Org by exact slug, else by the email's domain in member_domains."""
    if slug:
        org = (await db.execute(
            select(Organization).where(Organization.slug == slug.strip().lower())
        )).scalar_one_or_none()
        if org:
            return org
    domain = _domain(email or "")
    if domain:
        orgs = (await db.execute(select(Organization))).scalars().all()
        for org in orgs:
            if domain in (org.member_domains or []):
                return org
    return None


async def enforce_org_sso(db: AsyncSession, email: str,
                          sign_in_provider: str | None) -> None:
    """403 when the user's org enforces SSO and this token came from
    another provider. creator_emails are exempt — the admin who
    misconfigures SSO must still be able to log in and fix it."""
    domain = _domain(email)
    if not domain:
        return
    orgs = (await db.execute(select(Organization))).scalars().all()
    for org in orgs:
        if domain not in (org.member_domains or []):
            continue
        if not (org.sso_enforced and org.sso_provider_id):
            continue
        if (email or "").lower() in [e.lower() for e in (org.creator_emails or [])]:
            return
        if sign_in_provider != org.sso_provider_id:
            raise HTTPException(
                status_code=403,
                detail=f"{org.name} requires single sign-on. Sign in with your organization's identity provider.",
            )
        return
