"""AI-powered endpoints: summarize, NL search, find similar."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Document, User
from app.routers.auth import get_current_user
from app.services.ai import extract_similar_terms, generate_summary, nl_to_search_query
from app.services.search import search_documents

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/summarize/{doc_id}")
async def summarize_document(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Generate or retrieve an AI summary for a document."""
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Return cached summary if available
    if doc.summary:
        return {"summary": doc.summary, "cached": True}

    if not doc.text_content:
        raise HTTPException(status_code=400, detail="Document has no text content")

    from app.config import settings
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="AI service unavailable — DESCUBRE_ANTHROPIC_API_KEY not set. Restart backend with this env var.")

    summary = await generate_summary(doc.text_content)
    if not summary:
        raise HTTPException(status_code=503, detail="AI service returned no result")

    # Cache in DB
    doc.summary = summary
    await db.commit()

    return {"summary": summary, "cached": False}


@router.post("/nl-search")
async def natural_language_search(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Convert a natural language query to structured search and execute it."""
    nl_query = body.get("query", "").strip()
    if not nl_query:
        raise HTTPException(status_code=400, detail="Query is required")

    structured_query = await nl_to_search_query(nl_query)
    if not structured_query:
        raise HTTPException(status_code=503, detail="AI service unavailable")

    results, total = await search_documents(db, structured_query)

    return {
        "original_query": nl_query,
        "structured_query": structured_query,
        "results": results,
        "total": total,
    }


@router.post("/find-similar/{doc_id}")
async def find_similar(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Find documents similar to the given document."""
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if not doc.text_content:
        raise HTTPException(status_code=400, detail="Document has no text content")

    search_terms = await extract_similar_terms(doc.text_content)
    if not search_terms:
        raise HTTPException(status_code=503, detail="AI service unavailable")

    results, total = await search_documents(db, search_terms, per_page=20)

    # Filter out the source document
    results = [r for r in results if str(r["id"]) != str(doc_id)]

    return {
        "source_id": str(doc_id),
        "search_terms": search_terms,
        "results": results,
        "total": len(results),
    }
