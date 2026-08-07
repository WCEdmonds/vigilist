"""Issuing and revoking agent API keys.

Firebase-authenticated and manager-gated: minting a credential that reads a
matter's documents is an access grant, so it sits at the same bar as adding a
reviewer. The token itself is returned exactly once, from the create call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import AgentApiKey, ProductionSet, User
from app.routers.auth import get_current_user
from app.schemas import AgentApiKeyCreate, AgentApiKeyCreated, AgentApiKeyOut
from app.services.agent_keys import generate_token
from app.services.audit import log_action

router = APIRouter(prefix="/api", tags=["agent-keys"])


async def _require_manager(db: AsyncSession, user: User, production_id: int) -> None:
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")


def _key_out(key: AgentApiKey) -> AgentApiKeyOut:
    return AgentApiKeyOut(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        production_id=key.production_id,
        production_set_id=key.production_set_id,
        role=key.role,
        created_by=key.created_by,
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
    )


@router.post(
    "/productions/{production_id}/agent-keys",
    response_model=AgentApiKeyCreated,
    status_code=201,
)
async def create_agent_key(
    production_id: int,
    body: AgentApiKeyCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mint an agent API key. The token is shown once and never again."""
    await _require_manager(db, user, production_id)

    if body.production_set_id is not None:
        ps = await db.get(ProductionSet, body.production_set_id)
        if ps is None or ps.production_id != production_id:
            raise HTTPException(
                status_code=404, detail="Production set not found in this production"
            )

    expires_at = None
    if body.expires_in_days is not None:
        # Stored naive-UTC to match the column and the comparison in
        # agent_keys.authenticate.
        expires_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(days=body.expires_in_days)
        )

    token, prefix, digest = generate_token()
    key = AgentApiKey(
        name=body.name.strip(),
        key_prefix=prefix,
        key_hash=digest,
        production_id=production_id,
        production_set_id=body.production_set_id,
        role="readonly",  # the agent API is read-only; nothing else is honoured
        created_by=user.id,
        expires_at=expires_at,
    )
    db.add(key)
    await db.flush()

    await log_action(
        db, user, "agent_key_created", "agent_key", str(key.id),
        details={
            "name": key.name,
            "production_id": production_id,
            "production_set_id": body.production_set_id,
            "key_prefix": prefix,
        },
    )
    await db.commit()
    await db.refresh(key)

    return AgentApiKeyCreated(**_key_out(key).model_dump(), token=token)


@router.get(
    "/productions/{production_id}/agent-keys",
    response_model=list[AgentApiKeyOut],
)
async def list_agent_keys(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Every key issued for this matter, live and revoked."""
    await _require_manager(db, user, production_id)
    rows = (await db.execute(
        select(AgentApiKey)
        .where(AgentApiKey.production_id == production_id)
        .order_by(AgentApiKey.created_at.desc())
    )).scalars().all()
    return [_key_out(k) for k in rows]


@router.delete("/agent-keys/{key_id}", response_model=AgentApiKeyOut)
async def revoke_agent_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Revoke a key. Takes effect on the agent's next request."""
    key = await db.get(AgentApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Agent key not found")
    await _require_manager(db, user, key.production_id)

    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        key.revoked_by = user.id
        await log_action(
            db, user, "agent_key_revoked", "agent_key", str(key.id),
            details={"name": key.name, "key_prefix": key.key_prefix},
        )
    await db.commit()
    await db.refresh(key)
    return _key_out(key)
