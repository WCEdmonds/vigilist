"""AI-powered document classification for review workflows."""

import asyncio
import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)

# The Anthropic SDK's transient/retryable error types, resolved lazily (on
# first use) and cached here. classify_document catches this tuple rather
# than importing `anthropic.RateLimitError` etc. directly for two reasons:
#   1. It keeps the `anthropic` SDK off the module's import path, matching
#      the existing lazy-import-inside-the-function convention below.
#   2. It makes retry behavior testable without constructing real SDK error
#      instances (RateLimitError & friends require a synthetic httpx.Response
#      to build) — tests just monkeypatch this module attribute to a plain
#      exception type, e.g. `(ValueError,)`.
# Falls back to an empty tuple (nothing treated as retryable) if the SDK
# can't be imported for some reason, so classify_document still degrades to
# its existing immediate-sentinel behavior.
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] | None = None


def _retryable_errors() -> tuple[type[BaseException], ...]:
    global _RETRYABLE_ERRORS
    if _RETRYABLE_ERRORS is None:
        try:
            import anthropic
            _RETRYABLE_ERRORS = (
                anthropic.RateLimitError,
                anthropic.APIStatusError,
                anthropic.APIConnectionError,
            )
        except Exception:
            _RETRYABLE_ERRORS = ()
    return _RETRYABLE_ERRORS


_CLASSIFY_MAX_ATTEMPTS = 3

# Two-pass cascade: SCREEN_MODEL reads every document; CONFIRM_MODEL re-reads
# (and its answer wins) unless the screen pass returned a screen-out decision
# at or above the confidence threshold. Projects with custom categories that
# lack a screen-out decision escalate everything — the cascade only ever
# saves cost, never lets the cheap model be the last word on a document that
# might matter.
SCREEN_MODEL = "claude-haiku-4-5"
CONFIRM_MODEL = "claude-sonnet-4-6"
SCREEN_OUT_DECISIONS = {"not_relevant"}
SCREEN_CONFIDENCE_THRESHOLD = 80

DEFAULT_CATEGORIES = [
    {"name": "relevant", "color": "green", "description": "Supports our case theory or relates to key issues"},
    {"name": "key_document", "color": "blue", "description": "Particularly significant, needs attorney attention"},
    {"name": "not_relevant", "color": "gray", "description": "Not useful to our case"},
    {"name": "needs_review", "color": "yellow", "description": "Ambiguous, attorney should examine manually"},
]


def build_system_prompt(categories: list[dict]) -> str:
    cat_list = "\n".join(f'- "{c["name"]}": {c.get("description", c["name"])}' for c in categories)
    cat_names = [f'"{c["name"]}"' for c in categories]
    return f"""You are a legal document review assistant. You classify documents based on the attorney's review criteria.

You MUST respond with a JSON object containing exactly these fields:
{{
  "decision": {" | ".join(cat_names)},
  "confidence": 0-100,
  "reasoning": "2-4 sentence explanation",
  "key_excerpts": [{{"text": "exact quote from document", "start_offset": 0, "end_offset": 50}}],
  "considerations": "any caveats or notes for the reviewer"
}}

Category definitions:
{cat_list}

Rules:
- confidence 0-100: how certain you are about your decision
- key_excerpts: quote the EXACT text passages that informed your decision, with character offsets
- Be conservative: when in doubt, use "needs_review"
- Respond with ONLY the JSON object, no other text"""


def build_classification_prompt(review_criteria: str, document_text: str, categories: list[dict] | None = None) -> str:
    cats = categories or DEFAULT_CATEGORIES
    cat_names = ", ".join(c["name"] for c in cats)
    truncated = document_text[:12000]
    return f"""## Review Criteria

{review_criteria}

## Document Text

{truncated}

Classify this document as one of [{cat_names}] according to the review criteria above. Respond with JSON only."""


def parse_classification_response(raw: str) -> dict:
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        data = json.loads(cleaned)
        return {
            "decision": data.get("decision", "needs_review"),
            "confidence": max(0, min(100, int(data.get("confidence", 50)))),
            "reasoning": data.get("reasoning", "No reasoning provided."),
            "key_excerpts": data.get("key_excerpts", []),
            "considerations": data.get("considerations"),
        }
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse classification response: %s", e)
        return {
            "decision": "needs_review",
            "confidence": 0,
            "reasoning": f"Failed to parse AI response: {raw[:200]}",
            "key_excerpts": [],
            "considerations": "AI response could not be parsed. Manual review required.",
        }


async def classify_document(
    review_criteria: str,
    document_text: str,
    categories: list[dict] | None = None,
    model: str = "claude-sonnet-4-6",
) -> tuple[dict, int]:
    if not settings.anthropic_api_key:
        return parse_classification_response("{}"), 0

    cats = categories or DEFAULT_CATEGORIES
    import anthropic  # lazy: keep the SDK off the startup path

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    prompt = build_classification_prompt(review_criteria, document_text, cats)
    retryable = _retryable_errors()

    # Bounded retry: transient SDK errors (rate limits, API-side 5xx,
    # connection drops) get a few attempts with backoff before we give up.
    # Any other exception is treated as non-retryable and falls straight
    # through to the sentinel — the worker's `tokens == 0` contract is
    # unchanged either way.
    for attempt in range(_CLASSIFY_MAX_ATTEMPTS):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=1000,
                system=build_system_prompt(cats),
                messages=[{"role": "user", "content": prompt}],
            )

            raw_text = ""
            for block in response.content:
                if block.type == "text":
                    raw_text = block.text
                    break

            total_tokens = response.usage.input_tokens + response.usage.output_tokens
            result = parse_classification_response(raw_text)
            return result, total_tokens

        except retryable as e:
            status_code = getattr(e, "status_code", None)
            if status_code is not None and status_code not in (408, 429) and status_code < 500:
                # Non-transient API error (e.g. 401 invalid key, 400 bad request):
                # retrying won't help, so give up immediately rather than
                # burning attempts + backoff on a request that will never succeed.
                logger.error("Classification failed with non-retryable status %s: %s", status_code, e)
                break
            logger.warning(
                "Classification attempt %d/%d failed (retryable): %s",
                attempt + 1, _CLASSIFY_MAX_ATTEMPTS, e,
            )
            if attempt < _CLASSIFY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(2 * (attempt + 1))
        except Exception as e:
            logger.error("Classification failed: %s", e)
            return parse_classification_response("{}"), 0

    logger.error("Classification failed after %d retryable attempts", _CLASSIFY_MAX_ATTEMPTS)
    return parse_classification_response("{}"), 0


async def classify_document_cascade(
    review_criteria: str,
    document_text: str,
    categories: list[dict] | None = None,
) -> tuple[dict, int, str]:
    """Classify with the screen model, escalating to the confirm model.

    Returns (result, total_tokens, model) where model is the one whose answer
    is being returned. Follows classify_document's failure contract: tokens
    == 0 means no real answer was produced — including when the confirm pass
    fails after a successful screen, so a document the screen flagged as
    possibly relevant is never recorded on the cheap model's say-so.
    """
    screen, screen_tokens = await classify_document(
        review_criteria, document_text, categories=categories, model=SCREEN_MODEL
    )
    if screen_tokens == 0:
        return screen, 0, SCREEN_MODEL

    if (
        screen["decision"] in SCREEN_OUT_DECISIONS
        and screen["confidence"] >= SCREEN_CONFIDENCE_THRESHOLD
    ):
        return screen, screen_tokens, SCREEN_MODEL

    confirm, confirm_tokens = await classify_document(
        review_criteria, document_text, categories=categories, model=CONFIRM_MODEL
    )
    if confirm_tokens == 0:
        return confirm, 0, CONFIRM_MODEL
    return confirm, screen_tokens + confirm_tokens, CONFIRM_MODEL
