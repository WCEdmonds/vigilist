from fastapi import HTTPException
from sqlalchemy import select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Production, ProductionAccess, User


async def get_accessible_production_ids(
    db: AsyncSession, user: User
) -> list[int]:
    """Return production IDs the user owns or has access to."""
    owned = select(Production.id).where(Production.owner_id == user.id)
    granted = select(ProductionAccess.production_id).where(
        ProductionAccess.user_id == user.id
    )
    result = await db.execute(union_all(owned, granted))
    return [row[0] for row in result.all()]


async def get_user_role_for_production(
    db: AsyncSession, user: User, production_id: int
) -> str:
    """Return the user's role for a production. Owners get 'admin'."""
    prod = await db.get(Production, production_id)
    if prod and prod.owner_id == user.id:
        return "admin"

    result = await db.execute(
        select(ProductionAccess.role).where(
            ProductionAccess.production_id == production_id,
            ProductionAccess.user_id == user.id,
        )
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=403, detail="No access to this production")
    return role


ROLE_RANK = {"admin": 4, "manager": 3, "reviewer": 2, "readonly": 1}
