"""FastAPI dependency that authenticates an agent API key.

Separate from `app.routers.auth.get_current_user`, which speaks Firebase ID
tokens and returns a `User`. An agent has no user identity; it has a scope.
Nothing here ever falls back to the Firebase path, so an agent key cannot be
used to reach a user-only endpoint and vice versa.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.agent_keys import AgentScope, authenticate

_UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="Missing or invalid agent API key",
    headers={"WWW-Authenticate": "Bearer"},
)


def extract_token(authorization: str, api_key_header: str) -> str:
    """Pull a token from either accepted header, preferring X-API-Key.

    Both forms are supported because agent frameworks differ on which they can
    set: some only expose a bearer-token field, others only a custom header.
    """
    if api_key_header:
        return api_key_header.strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


async def get_agent_scope(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AgentScope:
    """Authenticate the caller and return what it may reach.

    Raises 401 for every failure — unknown, revoked, expired, malformed —
    without saying which, so the endpoint cannot be used to confirm that a
    given key prefix exists.
    """
    token = extract_token(
        request.headers.get("authorization", ""),
        request.headers.get("x-api-key", ""),
    )
    if not token:
        raise _UNAUTHORIZED

    scope = await authenticate(db, token)
    if scope is None:
        raise _UNAUTHORIZED

    # authenticate() stamped last_used_at on the row; persist it here so every
    # agent request records activity even on read-only endpoints.
    await db.commit()
    return scope
