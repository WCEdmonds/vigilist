"""classify_document: bounded retry around the Anthropic API call.

classify_document lazily imports the `anthropic` SDK inside the function
body (to keep the SDK off the startup import path), and the SDK's retryable
error types (RateLimitError, APIStatusError, APIConnectionError) all require
a synthetic httpx.Response to construct — awkward and brittle to build in a
unit test. So production code catches a module-level `_RETRYABLE_ERRORS`
tuple, lazily resolved from the real SDK error classes on first use and
cached. These tests monkeypatch that tuple directly to `(ValueError,)`, so a
plain ValueError stands in for "a retryable SDK error" without needing the
real SDK types at all.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import app.services.ai_review as ai_review


class FakeMessages:
    def __init__(self, side_effect):
        self.create = AsyncMock(side_effect=side_effect)


class FakeClient:
    def __init__(self, side_effect):
        self.messages = FakeMessages(side_effect)


def _mock_response(decision="relevant"):
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps({"decision": decision, "confidence": 80, "reasoning": "ok"})
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock(input_tokens=100, output_tokens=50)
    return resp


def _patch_client(monkeypatch, client):
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kwargs: client)


def _patch_common(monkeypatch):
    monkeypatch.setattr(ai_review.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(ai_review, "_RETRYABLE_ERRORS", (ValueError,))
    # Retries sleep 2s/4s between attempts — mock so the test doesn't stall.
    monkeypatch.setattr(ai_review.asyncio, "sleep", AsyncMock())


def test_classify_document_retries_then_succeeds(monkeypatch):
    _patch_common(monkeypatch)
    client = FakeClient(side_effect=[ValueError("rate limited"), ValueError("rate limited"), _mock_response()])
    _patch_client(monkeypatch, client)

    result, tokens = asyncio.run(ai_review.classify_document("criteria", "doc text"))

    assert client.messages.create.call_count == 3
    assert tokens == 150
    assert result["decision"] == "relevant"


def test_classify_document_falls_through_to_sentinel_after_exhausting_retries(monkeypatch):
    _patch_common(monkeypatch)
    client = FakeClient(side_effect=[ValueError("rate limited")] * 3)
    _patch_client(monkeypatch, client)

    result, tokens = asyncio.run(ai_review.classify_document("criteria", "doc text"))

    assert client.messages.create.call_count == 3
    assert tokens == 0
    assert result["decision"] == "needs_review"


def test_classify_document_does_not_retry_non_retryable_errors(monkeypatch):
    _patch_common(monkeypatch)
    client = FakeClient(side_effect=[RuntimeError("boom")])
    _patch_client(monkeypatch, client)

    result, tokens = asyncio.run(ai_review.classify_document("criteria", "doc text"))

    assert client.messages.create.call_count == 1
    assert tokens == 0
    assert result["decision"] == "needs_review"


class FakeAPIStatusError(Exception):
    """Stand-in for an SDK error with a `status_code`, e.g. a revoked API key.

    Real `anthropic.APIStatusError` instances require a synthetic
    httpx.Response to construct, so tests monkeypatch `_RETRYABLE_ERRORS` to
    include this plain exception subclass instead.
    """

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def test_classify_document_does_not_retry_non_transient_status_code(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(ai_review, "_RETRYABLE_ERRORS", (FakeAPIStatusError,))
    client = FakeClient(side_effect=[FakeAPIStatusError("invalid api key", status_code=401)])
    _patch_client(monkeypatch, client)

    result, tokens = asyncio.run(ai_review.classify_document("criteria", "doc text"))

    assert client.messages.create.call_count == 1
    assert tokens == 0
    assert result["decision"] == "needs_review"
