"""Thin wrapper over the Claude Message Batches API.

Large per-document runs (summaries, entity extraction) go through batches at
half price. A batch is submitted once and polled later; results come back in
any order, so they are always keyed by ``custom_id``, never by position.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

# Result types other than "succeeded" (errored, canceled, expired) map to
# None: the document stays unprocessed and a later run picks it up.
_SUCCEEDED = "succeeded"


def _client():
    # Lazy: keep the SDK off the startup path (same as services/ai.py).
    import anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("No Anthropic API key configured")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def submit(requests: list[tuple[str, dict]]) -> str:
    """Create one batch from ``(custom_id, params)`` pairs; returns its id.

    ``params`` are ordinary Messages API parameters. Each ``custom_id`` must be
    unique within the batch.
    """
    client = _client()
    batch = await client.messages.batches.create(
        requests=[{"custom_id": cid, "params": params} for cid, params in requests]
    )
    logger.info("Submitted message batch %s with %d requests", batch.id, len(requests))
    return batch.id


def _first_text(message) -> str | None:
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    return None


async def fetch_if_done(batch_id: str) -> dict[str, str | None] | None:
    """None while the batch is still processing; otherwise ``custom_id ->
    text`` for every request, with None for an errored, canceled or expired
    request (or a success with no text block)."""
    client = _client()
    batch = await client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        return None
    out: dict[str, str | None] = {}
    async for item in await client.messages.batches.results(batch_id):
        result = item.result
        if result.type == _SUCCEEDED:
            out[item.custom_id] = _first_text(result.message)
        else:
            out[item.custom_id] = None
    failed = sum(1 for v in out.values() if v is None)
    logger.info("Message batch %s ended: %d results, %d without text", batch_id, len(out), failed)
    return out
