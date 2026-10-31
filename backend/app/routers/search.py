import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids
from app.services.audit import log_action
from app.schemas import SearchResponse, SearchResult
from app.services.search import resolve_tz, search_documents
from app.services.semantic_search import semantic_search

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = "",
    production_id: int | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("relevance", pattern="^(relevance|bates|date_asc|date_desc)$"),
    mode: str = Query("fulltext", pattern="^(fulltext|semantic)$"),
    metadata: str | None = Query(None, description="JSON object of metadata key-value filters"),
    file_type: str | None = Query(None, description="Filter by file type: video, audio, pdf, office, image, email, native, images_only"),
    source_party: str | None = Query(None, description="Filter by source party label"),
    source_type: str | None = Query(None, pattern="^(collection|received)$"),
    email_address: str | None = Query(None, description="Email address or @domain to match"),
    email_scope: str = Query("from", pattern="^(from|any)$"),
    date_from: date | None = Query(None, description="Inclusive start date (YYYY-MM-DD)"),
    date_to: date | None = Query(None, description="Inclusive end date (YYYY-MM-DD)"),
    tz: str | None = Query(None, max_length=64, description="Reviewer's IANA timezone for date bounds"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    metadata_filters = None
    if metadata:
        try:
            metadata_filters = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid metadata JSON")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from is after date_to")
    try:
        resolve_tz(tz)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    accessible = await get_accessible_production_ids(db, user)

    results: list[dict] = []
    total = 0
    # Vector search over document chunks. Only when no metadata/file-type
    # filters are active (the semantic path doesn't support them), and falls
    # through to full-text when it yields nothing (e.g. embeddings not
    # available — no Voyage key or not yet generated).
    has_email = bool(email_address and email_address.strip())
    has_dates = date_from is not None or date_to is not None
    if (mode == "semantic" and q.strip() and not metadata_filters and not file_type
            and not source_party and not source_type and not has_email and not has_dates):
        results, total = await semantic_search(
            db, q, production_id=production_id, page=page, per_page=per_page,
            accessible_production_ids=accessible,
        )

    if not results:
        results, total = await search_documents(
            db, q, production_id=production_id, page=page, per_page=per_page, sort=sort,
            accessible_production_ids=accessible,
            metadata_filters=metadata_filters,
            file_type=file_type,
            source_party=source_party,
            source_type=source_type,
            email_address=email_address,
            email_scope=email_scope,
            date_from=date_from,
            date_to=date_to,
            tz=tz,
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
