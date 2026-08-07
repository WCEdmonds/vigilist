"""Minting and verification of agent API keys.

The token format is ``vgl_<prefix>_<secret>``: the prefix is stored in the
clear so a key can be located in one indexed lookup, the secret never is.
Only the SHA-256 of the whole token is persisted, so a database read cannot
recover a working credential.

SHA-256 rather than a password hash (bcrypt/argon2) is deliberate: the secret
is 32 bytes of ``secrets.token_urlsafe`` entropy, not a human-chosen password,
so there is nothing for a slow KDF to defend against — and this runs on every
agent request, where a deliberately slow hash would be a real cost.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentApiKey

TOKEN_NAMESPACE = "vgl"
# Hex, not urlsafe-base64: the prefix is a field in a `_`-delimited token, and
# base64url's alphabet includes `_`, which would make the boundary ambiguous.
_PREFIX_BYTES = 4   # 8 hex chars
_SECRET_BYTES = 32  # ~43 urlsafe chars; may contain `_`, hence the bounded split


@dataclass(frozen=True)
class AgentScope:
    """What an authenticated key is allowed to reach.

    `production_set_id` is None for a matter-wide key. Everything the agent
    API does derives its filters from this object — no endpoint takes a
    production or set id from the caller, so scope cannot be widened by
    request parameters.
    """

    key_id: int
    name: str
    production_id: int
    production_set_id: int | None
    role: str

    @property
    def is_set_scoped(self) -> bool:
        return self.production_set_id is not None


def hash_token(token: str) -> str:
    """SHA-256 hex of a full token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Mint a new token.

    Returns (token, key_prefix, key_hash). The token is the only copy of the
    secret — callers must hand it to the user and then drop it.
    """
    prefix = secrets.token_hex(_PREFIX_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    token = f"{TOKEN_NAMESPACE}_{prefix}_{secret}"
    return token, prefix, hash_token(token)


def parse_prefix(token: str) -> str | None:
    """Pull the lookup prefix out of a token, or None if it is malformed.

    Bounded split: the secret is base64url and may itself contain `_`, so only
    the first two separators delimit fields.
    """
    parts = (token or "").split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_NAMESPACE:
        return None
    if not parts[1] or not parts[2]:
        return None
    return parts[1]


def _is_expired(key: AgentApiKey, now: datetime) -> bool:
    if key.expires_at is None:
        return False
    expires = key.expires_at
    # Rows come back naive (the column is a bare DateTime) but a caller-set
    # value may carry a tzinfo; normalise before comparing so this can never
    # raise on the auth path.
    if expires.tzinfo is not None:
        expires = expires.astimezone(timezone.utc).replace(tzinfo=None)
    return expires <= now


async def authenticate(db: AsyncSession, token: str) -> AgentScope | None:
    """Resolve a token to its scope, or None if it is not a live credential.

    Returns None for every failure mode — malformed, unknown, revoked,
    expired — so callers cannot distinguish them and probe for valid prefixes.
    """
    prefix = parse_prefix(token)
    if prefix is None:
        return None

    candidates = (
        await db.execute(
            select(AgentApiKey).where(AgentApiKey.key_prefix == prefix)
        )
    ).scalars().all()
    if not candidates:
        return None

    digest = hash_token(token)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for key in candidates:
        # compare_digest, not ==, so a match cannot be found byte-by-byte from
        # response timing. Both sides are hex of a fixed length.
        if not hmac.compare_digest(key.key_hash, digest):
            continue
        if key.revoked_at is not None or _is_expired(key, now):
            return None
        # Best-effort usage stamp. The caller commits; if it doesn't, the key
        # still authenticated — this is telemetry, not part of the decision.
        key.last_used_at = now
        return AgentScope(
            key_id=key.id,
            name=key.name,
            production_id=key.production_id,
            production_set_id=key.production_set_id,
            role=key.role,
        )
    return None
