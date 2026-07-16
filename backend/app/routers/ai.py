"""AI-powered endpoints: summarize, NL search, find similar, chat."""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Document, User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids
from app.services.ai import (
    CHAT_MODEL,
    build_chat_system_prompt,
    extract_similar_terms,
    generate_summary,
    nl_to_search_query,
)
from app.services.search import search_documents

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

# Cap how many documents can be attached to a single chat request, so a user
# can't blow up the context window (and cost) by selecting an entire production.
_MAX_CHAT_DOCS = 25
# Cap conversation history length to keep requests bounded.
_MAX_CHAT_MESSAGES = 40


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


@router.post("/chat")
async def chat(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Stream a chat response from the AI agent, optionally grounded in attached documents.

    Request body:
      - messages: [{ "role": "user" | "assistant", "content": str }, ...]
      - doc_ids:  optional list of document UUIDs to attach as context.

    Responses stream as Server-Sent Events with JSON payloads:
      { "type": "delta", "text": str } | { "type": "done" } | { "type": "error", "message": str }
    """
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="AI service unavailable — VIGILIST_ANTHROPIC_API_KEY not set.",
        )

    # Sanitize the conversation history: only user/assistant turns with string content.
    raw_messages = body.get("messages") or []
    messages: list[dict] = []
    for m in raw_messages[-_MAX_CHAT_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})

    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="A user message is required")

    # Resolve any attached documents, enforcing production-level access control.
    accessible = await get_accessible_production_ids(db, user)
    doc_ids = body.get("doc_ids") or []
    documents: list[Document] = []
    for raw_id in doc_ids[:_MAX_CHAT_DOCS]:
        try:
            doc = await db.get(Document, UUID(str(raw_id)))
        except (ValueError, AttributeError):
            continue
        if doc and doc.production_id in accessible:
            documents.append(doc)

    system = build_chat_system_prompt(documents)

    async def event_stream():
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        try:
            async with client.messages.stream(
                model=CHAT_MODEL,
                max_tokens=4096,
                system=system,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield f"data: {json.dumps({'type': 'delta', 'text': text})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception:
            logger.warning("AI chat stream failed", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': 'The AI service failed to respond.'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
