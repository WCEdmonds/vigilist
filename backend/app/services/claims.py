"""Sync Firebase custom claims with production access.

Firebase Storage rules use custom claims to enforce per-production
access control. Whenever a user's production access changes (login,
invite, revoke, ingest), call sync_user_claims() to update their
Firebase token claims.

Custom claims are limited to 1000 bytes. For a small team with a
handful of productions this is fine. If the list grows large, switch
to a different authorization model (e.g., Cloud Functions + Firestore).
"""

import logging

from firebase_admin import auth as firebase_auth
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_accessible_production_ids
from app.models import User

logger = logging.getLogger(__name__)


async def sync_user_claims(db: AsyncSession, user: User) -> None:
    """Update Firebase custom claims with the user's accessible production IDs."""
    try:
        prod_ids = await get_accessible_production_ids(db, user)
        firebase_auth.set_custom_user_claims(user.id, {
            "production_ids": prod_ids,
        })
        logger.info("Synced claims for user %s: %d productions", user.email, len(prod_ids))
    except Exception:
        logger.exception("Failed to sync claims for user %s", user.email)
