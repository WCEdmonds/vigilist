"""extract_document_entities: the LLM-call half of entity_extraction.py.

Mirrors tests/test_classify_retry.py's pattern: the module lazily imports the
`anthropic` SDK inside the function body (to keep the SDK off the startup
import path) and catches a module-level `_RETRYABLE_ERRORS` tuple, lazily
resolved from the real SDK error classes on first use and cached. Tests
monkeypatch that tuple directly to plain exception types so a synthetic
error stands in for "a retryable SDK error" without needing to construct
real SDK exception instances (which require a synthetic httpx.Response).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import app.services.entity_extraction as entity_extraction


class FakeMessages:
    def __init__(self, side_effect):
        self.create = AsyncMock(side_effect=side_effect)


class FakeClient:
    def __init__(self, side_effect):
        self.messages = FakeMessages(side_effect)


def _mock_response(text=None, payload=None):
    if text is None:
        text = json.dumps(payload if payload is not None else {
            "entities": [{"name": "Jorge Rivera", "type": "person",
                          "surface_forms": ["Jorge Rivera"], "role": None, "emails": []}],
            "events": [], "relationships": [],
        })
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _patch_client(monkeypatch, client):
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kwargs: client)


def _patch_common(monkeypatch, client):
    monkeypatch.setattr(entity_extraction.settings, "anthropic_api_key", "test-key")
    # Retries sleep 2s/4s between attempts — mock so the test doesn't stall.
    monkeypatch.setattr(entity_extraction._asyncio, "sleep", AsyncMock())
    _patch_client(monkeypatch, client)


class FakeAPIStatusError(Exception):
    """Stand-in for an SDK error with a `status_code`, e.g. a revoked API key.

    Real `anthropic.APIStatusError` instances require a synthetic
    httpx.Response to construct, so tests monkeypatch `_RETRYABLE_ERRORS` to
    include this plain exception subclass instead.
    """

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def test_no_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(entity_extraction.settings, "anthropic_api_key", "")
    result = asyncio.run(entity_extraction.extract_document_entities("some text"))
    assert result is None


def test_valid_json_response_returns_parsed_dict(monkeypatch):
    client = FakeClient(side_effect=[_mock_response()])
    _patch_common(monkeypatch, client)

    result = asyncio.run(entity_extraction.extract_document_entities("some text"))

    assert client.messages.create.call_count == 1
    assert result is not None
    assert result["entities"][0]["name"] == "Jorge Rivera"


def test_empty_text_response_returns_sentinel_not_none(monkeypatch):
    client = FakeClient(side_effect=[_mock_response(text="")])
    _patch_common(monkeypatch, client)

    result = asyncio.run(entity_extraction.extract_document_entities("some text"))

    assert result == {"entities": [], "events": [], "relationships": []}


def test_transient_error_then_success_retries_and_succeeds(monkeypatch):
    monkeypatch.setattr(entity_extraction, "_RETRYABLE_ERRORS", (ValueError,))
    client = FakeClient(side_effect=[ValueError("rate limited"), _mock_response()])
    _patch_common(monkeypatch, client)

    result = asyncio.run(entity_extraction.extract_document_entities("some text"))

    assert client.messages.create.call_count == 2
    assert result is not None
    assert result["entities"][0]["name"] == "Jorge Rivera"


def test_non_retryable_status_error_returns_none_without_retrying(monkeypatch):
    monkeypatch.setattr(entity_extraction, "_RETRYABLE_ERRORS", (FakeAPIStatusError,))
    client = FakeClient(side_effect=[FakeAPIStatusError("bad request", status_code=400)])
    _patch_common(monkeypatch, client)

    result = asyncio.run(entity_extraction.extract_document_entities("some text"))

    assert client.messages.create.call_count == 1
    assert result is None


def test_hard_failure_on_second_slice_returns_none_no_partial_merge(monkeypatch):
    # > 140k chars so slice_text splits into 2 overlapping slices; the first
    # slice succeeds but the second hard-fails (non-retryable) — the whole
    # call must return None, never a merge of just the first slice's result.
    text = "x" * 200_000
    client = FakeClient(side_effect=[_mock_response(), RuntimeError("boom")])
    _patch_common(monkeypatch, client)

    result = asyncio.run(entity_extraction.extract_document_entities(text))

    assert client.messages.create.call_count == 2
    assert result is None
