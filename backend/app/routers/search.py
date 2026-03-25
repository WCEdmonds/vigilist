from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from app.schemas import SearchResponse, SearchResult
from app.services.search import search_documents

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str,
    production_id: int | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("relevance", pattern="^(relevance|bates)$"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    results, total = await search_documents(
        db, q, production_id=production_id, page=page, per_page=per_page, sort=sort
    )
    return SearchResponse(
        results=[SearchResult(**r) for r in results],
        total=total,
        page=page,
        per_page=per_page,
    )
