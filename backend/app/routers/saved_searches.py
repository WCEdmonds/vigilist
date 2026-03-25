from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import SavedSearch, User
from app.routers.auth import get_current_user
from app.schemas import SavedSearchCreate, SavedSearchOut

router = APIRouter(prefix="/api/saved-searches", tags=["saved_searches"])


@router.get("", response_model=list[SavedSearchOut])
async def list_saved_searches(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    query = select(SavedSearch).order_by(SavedSearch.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.post("", response_model=SavedSearchOut)
async def create_saved_search(
    body: SavedSearchCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ss = SavedSearch(name=body.name, query=body.query, filters=body.filters, created_by=user.id)
    db.add(ss)
    await db.commit()
    await db.refresh(ss)
    return ss


@router.delete("/{search_id}")
async def delete_saved_search(
    search_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    ss = await db.get(SavedSearch, search_id)
    if not ss:
        raise HTTPException(status_code=404, detail="Saved search not found")
    await db.delete(ss)
    await db.commit()
    return {"ok": True}
