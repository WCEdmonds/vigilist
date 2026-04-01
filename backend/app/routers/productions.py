"""Production listing and access management."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids, get_user_role_for_production
from app.models import PendingInvite, Production, ProductionAccess, User
from app.routers.auth import get_current_user
from app.services.audit import log_action
from app.services.claims import sync_user_claims
from app.services.email import send_access_granted_email, send_invite_email
from app.schemas import (
    InviteRequest,
    PendingInviteOut,
    ProductionAccessOut,
    ProductionWithAccess,
)

router = APIRouter(prefix="/api/productions", tags=["productions"])


@router.get("", response_model=list[ProductionWithAccess])
async def list_productions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List productions the user owns or has access to."""
    prod_ids = await get_accessible_production_ids(db, user)
    if not prod_ids:
        return []
    result = await db.execute(
        select(Production)
        .where(Production.id.in_(prod_ids))
        .order_by(Production.created_at.desc())
    )
    prods = result.scalars().all()
    return [
        ProductionWithAccess(
            id=p.id,
            name=p.name,
            description=p.description,
            owner_id=p.owner_id,
            is_owner=(p.owner_id == user.id),
            created_at=p.created_at,
        )
        for p in prods
    ]


@router.get("/{production_id}/access", response_model=list[ProductionAccessOut])
async def list_access(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List users with access to a production. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = await db.execute(
        select(ProductionAccess, User)
        .join(User, ProductionAccess.user_id == User.id)
        .where(ProductionAccess.production_id == production_id)
        .order_by(ProductionAccess.granted_at)
    )
    rows = result.all()
    return [
        ProductionAccessOut(
            id=pa.id,
            user_id=pa.user_id,
            user_email=u.email,
            user_display_name=u.display_name,
            role=pa.role,
            granted_by=pa.granted_by,
            granted_at=pa.granted_at,
        )
        for pa, u in rows
    ]


@router.get("/{production_id}/invites", response_model=list[PendingInviteOut])
async def list_pending_invites(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List pending invites for a production. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = await db.execute(
        select(PendingInvite)
        .where(PendingInvite.production_id == production_id)
        .order_by(PendingInvite.created_at)
    )
    return result.scalars().all()


@router.post("/{production_id}/access")
async def invite_user(
    production_id: int,
    body: InviteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Invite a user by email. Creates access if user exists, pending invite if not."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    email = body.email.strip().lower()

    # Check if user already exists
    result = await db.execute(select(User).where(User.email == email))
    target_user = result.scalar_one_or_none()

    if target_user:
        # Check if already has access
        existing = await db.execute(
            select(ProductionAccess).where(
                ProductionAccess.production_id == production_id,
                ProductionAccess.user_id == target_user.id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="User already has access")

        pa = ProductionAccess(
            production_id=production_id,
            user_id=target_user.id,
            granted_by=user.id,
            role=body.role,
        )
        db.add(pa)
        await log_action(db, user, "user_invited", "production", str(production_id),
                         production_id=production_id, details={"email": body.email, "role": body.role})
        await db.commit()

        # Sync the invited user's Firebase claims
        await sync_user_claims(db, target_user)

        inviter_name = user.display_name or user.email
        send_access_granted_email(email, inviter_name, prod.name, body.role)

        return {"status": "granted", "email": email}
    else:
        # Create pending invite
        existing = await db.execute(
            select(PendingInvite).where(
                PendingInvite.production_id == production_id,
                PendingInvite.email == email,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Invite already pending")

        invite = PendingInvite(
            production_id=production_id,
            email=email,
            invited_by=user.id,
            role=body.role,
        )
        db.add(invite)
        await log_action(db, user, "user_invited", "production", str(production_id),
                         production_id=production_id, details={"email": body.email, "role": body.role})
        await db.commit()

        inviter_name = user.display_name or user.email
        send_invite_email(email, inviter_name, prod.name, body.role)

        return {"status": "invited", "email": email}


@router.delete("/{production_id}/access/{user_id}")
async def revoke_access(
    production_id: int,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Revoke a user's access. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = await db.execute(
        select(ProductionAccess).where(
            ProductionAccess.production_id == production_id,
            ProductionAccess.user_id == user_id,
        )
    )
    pa = result.scalar_one_or_none()
    if not pa:
        raise HTTPException(status_code=404, detail="Access entry not found")

    await db.delete(pa)
    await log_action(db, user, "access_revoked", "production", str(production_id),
                     production_id=production_id, details={"revoked_user_id": user_id})
    await db.commit()

    # Sync the revoked user's Firebase claims
    revoked_user = await db.get(User, user_id)
    if revoked_user:
        await sync_user_claims(db, revoked_user)

    return {"ok": True}
