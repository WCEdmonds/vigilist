import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids
from app.services.audit import log_action
from app.schemas import SearchResponse, SearchResult
from app.services.search import search_documents

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = "",
    production_id: int | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("relevance", pattern="^(relevance|bates)$"),
    metadata: str | None = Query(None, description="JSON object of metadata key-value filters"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    metadata_filters = None
    if metadata:
        try:
            metadata_filters = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid metadata JSON")

    accessible = await get_accessible_production_ids(db, user)
    results, total = await search_documents(
        db, q, production_id=production_id, page=page, per_page=per_page, sort=sort,
        accessible_production_ids=accessible,
        metadata_filters=metadata_filters,
    )
    await log_action(db, user, "search_executed", "search", None,
                     details={"query": q, "result_count": total})
    await db.commit()
    return SearchResponse(
        results=[SearchResult(**r) for r in results],
        total=total,
        page=page,
        per_page=per_page,
    )
