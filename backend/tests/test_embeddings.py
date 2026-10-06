from unittest.mock import MagicMock, patch
from app.services.embeddings import embed_texts, embed_query


def test_embed_texts_batching():
    with patch("app.services.embeddings._get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        client.embed.side_effect = [
            MagicMock(embeddings=[[0.1] * 1024] * 128),
            MagicMock(embeddings=[[0.1] * 1024] * 72),
        ]
        texts = [f"text {i}" for i in range(200)]
        result = embed_texts(texts)
        assert len(result) == 200
        assert client.embed.call_count == 2


def test_embed_texts_no_api_key():
    with patch("app.services.embeddings._get_client", return_value=None):
        result = embed_texts(["hello"])
        assert result == []


def test_embed_query_no_api_key():
    with patch("app.services.embeddings._get_client", return_value=None):
        result = embed_query("hello")
        assert result == []


def test_embed_query_uses_query_input_type():
    with patch("app.services.embeddings._get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        client.embed.return_value = MagicMock(embeddings=[[0.1] * 1024])
        embed_query("test query")
        _, kwargs = client.embed.call_args
        assert kwargs.get("input_type") == "query"


# ── Oversized requests fail individually (2026-10-06: a 637k-token batch
# was rejected and retried 3x, losing every chunk of the document) ──

from voyageai.error import InvalidRequestError

from app.services import embeddings as emb


def _echo_client(reject=lambda batch: False):
    """Fake Voyage client: one vector per input; raises for rejected batches."""
    client = MagicMock()

    def _embed(batch, **_kw):
        if reject(batch):
            raise InvalidRequestError("batch too large")
        return MagicMock(embeddings=[[float(len(t))] for t in batch])

    client.embed.side_effect = _embed
    return client


def test_batches_respect_token_budget():
    big = "x" * (emb.TOKEN_BUDGET_PER_CALL * emb.CHARS_PER_TOKEN // 2 + 10)
    client = _echo_client()
    with patch("app.services.embeddings._get_client", return_value=client):
        result = emb.embed_texts([big, big, big])
    assert len(result) == 3 and all(r is not None for r in result)
    assert client.embed.call_count == 3  # each over half the budget → own call


def test_rejected_item_fails_alone_without_retrying():
    poison = "POISON"
    client = _echo_client(reject=lambda batch: poison in batch)
    with patch("app.services.embeddings._get_client", return_value=client), \
         patch("app.services.embeddings.time.sleep") as sleep:
        result = emb.embed_texts(["a", "bb", poison, "dddd"])
    assert result[0] == [1.0] and result[1] == [2.0] and result[3] == [4.0]
    assert result[2] is None
    sleep.assert_not_called()


def test_chunk_and_embed_keeps_successful_chunks(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    doc = MagicMock(id="d1", text_content="unused")
    db = MagicMock()
    db.get = AsyncMock(return_value=doc)
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    added = []
    db.add = added.append
    monkeypatch.setattr(emb, "chunk_text", lambda t: ["c0", "c1", "c2"])
    monkeypatch.setattr(emb, "embed_texts", lambda chunks: [[0.1], None, [0.3]])

    n = asyncio.run(emb.chunk_and_embed_document(db, "d1"))
    assert n == 2
    assert [c.chunk_index for c in added] == [0, 2]
