"""AI-powered features using Claude API."""

import asyncio
import logging

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)


def _get_client() -> anthropic.AsyncAnthropic | None:
    if not settings.anthropic_api_key:
        return None
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _extract_text(response) -> str | None:
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return None


# ── Document Summarization ──

SUMMARY_PROMPT = """You are a legal document analyst. Summarize this litigation document in 2-4 sentences.
Focus on: document type, key parties involved, subject matter, important dates, and any notable facts or claims.
Be precise and objective. Use language suitable for a legal professional reviewing documents.

Respond with ONLY the summary, no preamble."""


async def generate_summary(text: str) -> str | None:
    """Generate a summary for a document from its extracted text."""
    client = _get_client()
    if not client or not text or not text.strip():
        return None

    # Use more text for summaries than titles — first ~3000 tokens
    truncated = text[:12000].strip()
    if not truncated:
        return None

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": f"{SUMMARY_PROMPT}\n\n---\n\n{truncated}"}],
        )
        return _extract_text(response)
    except Exception:
        logger.warning("Failed to generate summary", exc_info=True)
        return None


# ── Natural Language Search ──

NL_SEARCH_PROMPT = """You are a legal search assistant. Convert this natural language query into optimal PostgreSQL full-text search terms.

Rules:
- Output ONLY the search terms, no explanation
- Use quoted phrases for multi-word concepts: "breach of contract"
- Use AND/OR/NOT for boolean logic
- Use wildcard* for prefix matching
- Focus on the key legal/factual terms that would appear in documents
- Remove filler words, keep substantive terms
- If the query mentions a date range or party name, include those terms

Examples:
- "emails about the settlement between Smith and Jones" → "settlement" AND (Smith OR Jones) AND email*
- "any documents mentioning breach of fiduciary duty" → "breach of fiduciary duty"
- "depositions from 2024 about damages" → deposition* AND damage* AND 2024
- "correspondence regarding the lease agreement" → (letter* OR email* OR correspondence) AND "lease agreement"

Natural language query:"""


async def nl_to_search_query(nl_query: str) -> str | None:
    """Convert a natural language query into structured search terms."""
    client = _get_client()
    if not client or not nl_query or not nl_query.strip():
        return None

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": f"{NL_SEARCH_PROMPT}\n{nl_query}"}],
        )
        return _extract_text(response)
    except Exception:
        logger.warning("Failed to convert NL query", exc_info=True)
        return None


# ── Find Similar Documents ──

SIMILAR_PROMPT = """You are a legal document analyst. Extract the 5-8 most distinctive search terms or short phrases from this document that would help find similar documents in a litigation production.

Focus on: key legal concepts, party names, specific topics, document types, and unique subject matter.
Output ONLY the terms separated by OR, suitable for a full-text search query. Use quoted phrases for multi-word concepts.

Example output: "breach of contract" OR "fiduciary duty" OR Smith OR "settlement agreement" OR damages"""


async def extract_similar_terms(text: str) -> str | None:
    """Extract key terms from a document for finding similar documents."""
    client = _get_client()
    if not client or not text or not text.strip():
        return None

    truncated = text[:8000].strip()
    if not truncated:
        return None

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": f"{SIMILAR_PROMPT}\n\n---\n\n{truncated}"}],
        )
        return _extract_text(response)
    except Exception:
        logger.warning("Failed to extract similar terms", exc_info=True)
        return None


# ── Batch Title Generation (used during ingest) ──

TITLE_PROMPT = """Generate a concise descriptive title (max 80 characters) for this litigation document.
The title should capture: document type, key subject matter, and any identifiable parties or dates.
Examples of good titles:
- Email: Smith to Jones re Settlement Offer (Mar 2024)
- Deposition Transcript of Dr. Williams, Vol. 2
- Invoice from ABC Corp for Consulting Services
- Handwritten Notes on Contract Negotiations

IMPORTANT RULES:
- Respond with ONLY the title text, no quotes, no explanation
- NEVER say you don't have enough context. If the text is sparse or unclear, describe what you CAN see (e.g. "Blank Page" or "Single-Page Document Fragment")
- Keep it under 80 characters
- Do not wrap the title in quotation marks"""


async def generate_title(text: str) -> str | None:
    """Generate a title for a document from its extracted text."""
    client = _get_client()
    if not client or not text or not text.strip():
        return None

    truncated = text[:4000].strip()
    if not truncated:
        return None

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=120,
            messages=[{"role": "user", "content": f"{TITLE_PROMPT}\n\n---\n\n{truncated}"}],
        )
        title = _extract_text(response)
        if not title:
            return None
        # Strip surrounding quotes if the model added them
        title = title.strip('"\'')
        # Reject titles that are refusals
        refusal_phrases = ["i don't have enough", "i cannot", "insufficient", "not enough context", "unable to"]
        if any(p in title.lower() for p in refusal_phrases):
            return None
        # Truncate to fit DB column (200 chars) and display preference (80 chars)
        if len(title) > 200:
            title = title[:197] + "..."
        return title
    except Exception:
        logger.warning("Failed to generate title", exc_info=True)
        return None


async def generate_titles_batch(texts: list[tuple[str, str | None]]) -> dict[str, str | None]:
    """Generate titles for multiple documents with rate-limit-safe batching."""
    semaphore = asyncio.Semaphore(2)  # conservative to avoid rate limits

    async def _gen(doc_id: str, text: str | None) -> tuple[str, str | None]:
        async with semaphore:
            title = await generate_title(text or "")
            # Small delay between requests to stay under rate limits
            await asyncio.sleep(0.5)
            return doc_id, title

    tasks = [_gen(doc_id, text) for doc_id, text in texts]
    results = await asyncio.gather(*tasks)
    return dict(results)
