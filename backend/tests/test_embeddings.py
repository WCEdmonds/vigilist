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
