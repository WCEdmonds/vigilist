"""Text chunking for vector embeddings."""


def chunk_text(
    text: str | None,
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[str]:
    """Split text into word-based chunks with overlap.

    Args:
        text: Document text to chunk.
        chunk_size: Target words per chunk.
        overlap: Words of overlap between consecutive chunks.

    Returns:
        List of chunk strings.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    if len(words) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        if end >= len(words):
            break
        start = end - overlap

    return chunks
