"""AI-powered endpoints: summarize, NL search, find similar, chat."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Document, User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids
from app.services.ai import extract_similar_terms, generate_summary, nl_to_search_query, stream_chat
from app.services.search import search_documents

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/summarize/{doc_id}")
async def summarize_document(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate or retrieve an AI summary for a document."""
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    # Return cached summary if available
    if doc.summary:
        return {"summary": doc.summary, "cached": True}

    if not doc.text_content:
        raise HTTPException(status_code=400, detail="Document has no text content")

    from app.config import settings
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="AI service unavailable — VIGILIST_ANTHROPIC_API_KEY not set. Restart backend with this env var.")

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
    user: User = Depends(get_current_user),
):
    """Convert a natural language query to structured search and execute it."""
    accessible = await get_accessible_production_ids(db, user)
    nl_query = body.get("query", "").strip()
    if not nl_query:
        raise HTTPException(status_code=400, detail="Query is required")

    structured_query = await nl_to_search_query(nl_query)
    if not structured_query:
        raise HTTPException(status_code=503, detail="AI service unavailable")

    results, total = await search_documents(db, structured_query, accessible_production_ids=accessible)

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
    user: User = Depends(get_current_user),
):
    """Find documents similar to the given document."""
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    if not doc.text_content:
        raise HTTPException(status_code=400, detail="Document has no text content")

    search_terms = await extract_similar_terms(doc.text_content)
    if not search_terms:
        raise HTTPException(status_code=503, detail="AI service unavailable")

    results, total = await search_documents(db, search_terms, per_page=20, accessible_production_ids=accessible)

    # Filter out the source document
    results = [r for r in results if str(r["id"]) != str(doc_id)]

    return {
        "source_id": str(doc_id),
        "search_terms": search_terms,
        "results": results,
        "total": len(results),
    }


MAX_CHAT_DOCS = 50


@router.post("/chat")
async def chat(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Stream an AI chat response grounded in a set of selected documents.

    Body: {"document_ids": [uuid, ...], "messages": [{"role", "content"}, ...]}.
    Returns a plain-text stream of the assistant's reply.
    """
    from app.config import settings
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="AI service unavailable — VIGILIST_ANTHROPIC_API_KEY not set. Restart backend with this env var.")

    raw_ids = body.get("document_ids") or []
    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    if not raw_ids:
        raise HTTPException(status_code=400, detail="Select at least one document to chat about")
    if len(raw_ids) > MAX_CHAT_DOCS:
        raise HTTPException(status_code=400, detail=f"Too many documents selected (max {MAX_CHAT_DOCS}). Narrow your selection.")

    try:
        doc_ids = [UUID(str(i)) for i in raw_ids]
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid document id")

    accessible = await get_accessible_production_ids(db, user)
    result = await db.execute(select(Document).where(Document.id.in_(doc_ids)))
    docs = [d for d in result.scalars().all() if d.production_id in accessible]
    if not docs:
        raise HTTPException(status_code=404, detail="No accessible documents found for the given ids")

    # Preserve the caller's selection order for stable citations.
    order = {str(i): n for n, i in enumerate(raw_ids)}
    docs.sort(key=lambda d: order.get(str(d.id), 0))

    documents = [
        {"bates_begin": d.bates_begin, "title": d.title, "text_content": d.text_content}
        for d in docs
    ]

    async def generate():
        async for chunk in stream_chat(documents, messages):
            yield chunk

    return StreamingResponse(
        generate(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
