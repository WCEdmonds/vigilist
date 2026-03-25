import firebase_admin
from firebase_admin import auth as firebase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import PendingInvite, ProductionAccess, User
from app.schemas import UserOut
from app.services.audit import log_action

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
    email = decoded.get("email", "")
    display_name = decoded.get("name", "")

    # Upsert user
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(id=uid, email=email, display_name=display_name)
        db.add(user)
        await db.flush()
    else:
        # Update email/name if changed in Firebase
        if user.email != email and email:
            user.email = email
        if user.display_name != display_name and display_name:
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
    return user


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return current user profile."""
    await db.commit()
    return user
