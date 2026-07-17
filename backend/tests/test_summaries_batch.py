"""generate_summaries_batch: batches per-document summaries with bounded concurrency."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.ai import generate_summaries_batch


def _mock_response(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def test_summaries_batch_returns_per_doc_results():
    with patch("app.services.ai._get_client") as mock_get:
        client = MagicMock()
        client.messages.create = AsyncMock(
            side_effect=[_mock_response("Summary A."), _mock_response("Summary B.")]
        )
        mock_get.return_value = client

        out = asyncio.run(
            generate_summaries_batch([("doc-1", "text one"), ("doc-2", "text two")])
        )

    assert out == {"doc-1": "Summary A.", "doc-2": "Summary B."}
    assert client.messages.create.call_count == 2


def test_summaries_batch_skips_empty_text_without_calling_model():
    with patch("app.services.ai._get_client") as mock_get:
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=_mock_response("Summary A."))
        mock_get.return_value = client

        out = asyncio.run(
            generate_summaries_batch([("doc-1", None), ("doc-2", ""), ("doc-3", "real text")])
        )

    assert out["doc-1"] is None
    assert out["doc-2"] is None
    assert out["doc-3"] == "Summary A."
    assert client.messages.create.call_count == 1
