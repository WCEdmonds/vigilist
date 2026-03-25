"""AI-powered document title generation using Claude."""

import logging

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

TITLE_PROMPT = """Generate a concise descriptive title (max 80 characters) for this litigation document.
The title should capture: document type, key subject matter, and any identifiable parties or dates.
Examples of good titles:
- "Email: Smith to Jones re Settlement Offer (Mar 2024)"
- "Deposition Transcript of Dr. Williams, Vol. 2"
- "Invoice from ABC Corp for Consulting Services"
- "Handwritten Notes on Contract Negotiations"

Respond with ONLY the title, no quotes, no explanation."""


async def generate_title(text: str) -> str | None:
    """Generate a title for a document from its extracted text.

    Sends the first ~1000 tokens (roughly 4000 chars) to Claude Haiku.
    Returns None if the API key is not configured or the call fails.
    """
    if not settings.anthropic_api_key:
        return None

    if not text or not text.strip():
        return None

    # Approximate first ~1000 tokens by taking first 4000 characters
    truncated = text[:4000].strip()
    if not truncated:
        return None

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=120,
            messages=[
                {
                    "role": "user",
                    "content": f"{TITLE_PROMPT}\n\n---\n\n{truncated}",
                }
            ],
        )
        for block in response.content:
            if block.type == "text":
                title = block.text.strip()
                # Enforce max length
                if len(title) > 80:
                    title = title[:77] + "..."
                return title
    except Exception:
        logger.warning("Failed to generate title", exc_info=True)

    return None


async def generate_titles_batch(texts: list[tuple[str, str | None]]) -> dict[str, str | None]:
    """Generate titles for multiple documents.

    Args:
        texts: List of (doc_id_str, text_content) tuples.

    Returns:
        Dict mapping doc_id_str to generated title (or None).
    """
    import asyncio

    semaphore = asyncio.Semaphore(5)  # Limit concurrent API calls

    async def _gen(doc_id: str, text: str | None) -> tuple[str, str | None]:
        async with semaphore:
            title = await generate_title(text or "")
            return doc_id, title

    tasks = [_gen(doc_id, text) for doc_id, text in texts]
    results = await asyncio.gather(*tasks)
    return dict(results)
