from fastapi import HTTPException
from sqlalchemy import select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization, Production, ProductionAccess, User

ROLE_RANK = {"admin": 4, "manager": 3, "reviewer": 2, "readonly": 1}


def email_domain(email: str | None) -> str:
    """Return the lowercase domain part of an email, or '' if not an email."""
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].strip().lower()


async def get_member_organizations(db: AsyncSession, user: User) -> list[Organization]:
    """Organizations the user is a member of, by email domain.

    Orgs are few (one per firm), so we load them all and match in Python
    rather than pushing an array-contains query into every access check.
    """
    domain = email_domain(user.email)
    if not domain:
        return []
    orgs = (await db.execute(select(Organization))).scalars().all()
    return [o for o in orgs if domain in (o.member_domains or [])]


async def get_org_production_ids(db: AsyncSession, org_ids: list[int]) -> set[int]:
    """Production IDs owned by the given organizations."""
    if not org_ids:
        return set()
    result = await db.execute(
        select(Production.id).where(Production.organization_id.in_(org_ids))
    )
    return {row[0] for row in result.all()}


async def get_accessible_production_ids(db: AsyncSession, user: User) -> list[int]:
    """Return production IDs the user owns, has been granted, or can reach
    through organization membership."""
    owned = select(Production.id).where(Production.owner_id == user.id)
    granted = select(ProductionAccess.production_id).where(
        ProductionAccess.user_id == user.id
    )
    result = await db.execute(union_all(owned, granted))
    ids = {row[0] for row in result.all()}

    member_orgs = await get_member_organizations(db, user)
    ids |= await get_org_production_ids(db, [o.id for o in member_orgs])

    return sorted(ids)


async def get_user_role_for_production(
    db: AsyncSession, user: User, production_id: int
) -> str:
    """Return the user's effective role for a production — the highest of any
    access path (ownership, explicit grant, org membership). Owners are admins.
    Raises 403 if the user has no access at all."""
    prod = await db.get(Production, production_id)
    if prod and prod.owner_id == user.id:
        return "admin"

    candidate_roles: list[str] = []

    result = await db.execute(
        select(ProductionAccess.role).where(
            ProductionAccess.production_id == production_id,
            ProductionAccess.user_id == user.id,
        )
    )
    explicit = result.scalar_one_or_none()
    if explicit is not None:
        candidate_roles.append(explicit)

    if prod is not None and prod.organization_id is not None:
        org = await db.get(Organization, prod.organization_id)
        if org and email_domain(user.email) in (org.member_domains or []):
            candidate_roles.append(org.member_role)

    if not candidate_roles:
        raise HTTPException(status_code=403, detail="No access to this production")

    return max(candidate_roles, key=lambda r: ROLE_RANK.get(r, 0))


async def resolve_org_for_creator(db: AsyncSession, user: User) -> int | None:
    """The organization a production created by this user should be filed under,
    or None. Matches the creator's email domain against member_domains, or the
    exact email against creator_emails."""
    email = (user.email or "").strip().lower()
    domain = email_domain(email)
    orgs = (await db.execute(select(Organization))).scalars().all()
    for o in orgs:
        if domain and domain in (o.member_domains or []):
            return o.id
        if email and email in [e.lower() for e in (o.creator_emails or [])]:
            return o.id
    return None
