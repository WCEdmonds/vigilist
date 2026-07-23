import firebase_admin
from firebase_admin import auth as firebase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Organization, PendingInvite, ProductionAccess, User
from app.schemas import UserOut
from app.services.audit import log_action
from app.services.sso import PROVIDER_ID_RE, enforce_org_sso, resolve_sso_org

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Initialize Firebase Admin SDK (once at import time)
if not firebase_admin._apps:
    try:
        firebase_admin.initialize_app()
    except Exception:
        if settings.firebase_project_id:
            firebase_admin.initialize_app(options={"projectId": settings.firebase_project_id})
        else:
            firebase_admin.initialize_app()


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and verify Firebase ID token, upsert user in DB."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]  # Strip "Bearer "
    try:
        decoded = firebase_auth.verify_id_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    uid = decoded["uid"]
    email = decoded.get("email") or ""
    display_name = decoded.get("name") or ""
    sign_in_provider = (decoded.get("firebase") or {}).get("sign_in_provider")

    # If the ID token didn't carry an email/name (common for anonymous sign-in
    # or custom tokens), try to pull them from the Firebase user record.
    if not email or not display_name:
        try:
            fb_user = firebase_auth.get_user(uid)
            if not email and fb_user.email:
                email = fb_user.email
            if not display_name and fb_user.display_name:
                display_name = fb_user.display_name
        except Exception:
            pass

    # If we still have no email, synthesize a unique placeholder so the
    # users.email unique constraint doesn't collide across anonymous users.
    if not email:
        email = f"noemail-{uid}@vigilist.local"

    # P4-1: orgs that enforce SSO reject tokens from other providers.
    await enforce_org_sso(db, email, sign_in_provider)

    # Upsert user
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(id=uid, email=email, display_name=display_name)
        db.add(user)
        await db.flush()
    else:
        # Update email/name if changed in Firebase. Also heal any legacy
        # rows whose stored email is empty (pre-fix anonymous users).
        if email and user.email != email:
            user.email = email
        if display_name and user.display_name != display_name:
            user.display_name = display_name
        await db.flush()

    return user


@router.post("/sync", response_model=UserOut)
async def sync_user(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Called after Firebase login to ensure user exists in backend DB.
    Also resolves any pending invites for this user's email.
    """
    # Resolve pending invites
    result = await db.execute(
        select(PendingInvite).where(PendingInvite.email == user.email.lower())
    )
    pending = result.scalars().all()
    for invite in pending:
        # Check if access already exists
        existing = await db.execute(
            select(ProductionAccess).where(
                ProductionAccess.production_id == invite.production_id,
                ProductionAccess.user_id == user.id,
            )
        )
        if not existing.scalar_one_or_none():
            db.add(ProductionAccess(
                production_id=invite.production_id,
                user_id=user.id,
                granted_by=invite.invited_by,
                role=invite.role,
            ))
        await db.delete(invite)

    await log_action(db, user, "user_login", "user", user.id)
    await db.commit()

    # Sync Firebase custom claims with current production access
    from app.services.claims import sync_user_claims
    await sync_user_claims(db, user)

    return user


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return current user profile."""
    await db.commit()
    return user


@router.get("/sso-config")
async def sso_config(
    slug: str | None = None,
    email: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Public login-page discovery: which provider (if any) serves this
    subdomain or email domain. Discloses only provider id + display name."""
    org = await resolve_sso_org(db, slug, email)
    if not org or not org.sso_provider_id:
        return {"provider_id": None, "enforced": False, "org_name": None}
    return {"provider_id": org.sso_provider_id,
            "enforced": bool(org.sso_enforced),
            "org_name": org.name}


@router.put("/organizations/{slug}/sso")
async def update_org_sso(
    slug: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Bind/enforce an Identity Platform provider for an org.

    Gated to creator_emails members (the org-admin escape hatch); audited.
    """
    org = (await db.execute(
        select(Organization).where(Organization.slug == slug.strip().lower())
    )).scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if user.email.lower() not in [e.lower() for e in (org.creator_emails or [])]:
        raise HTTPException(status_code=403, detail="Only organization administrators may configure SSO")

    provider_id = body.get("provider_id") or None
    enforced = bool(body.get("enforced", False))
    if provider_id is not None and not PROVIDER_ID_RE.match(provider_id):
        raise HTTPException(status_code=422, detail="provider_id must look like 'saml.name' or 'oidc.name'")
    if enforced and not provider_id:
        raise HTTPException(status_code=422, detail="cannot enforce SSO without a provider_id")

    org.sso_provider_id = provider_id
    org.sso_enforced = enforced
    await log_action(db, user, "org_sso_updated", "organization", str(org.id),
                     details={"slug": org.slug, "provider_id": provider_id,
                              "enforced": enforced})
    await db.commit()
    return {"slug": org.slug, "provider_id": provider_id, "enforced": enforced}
