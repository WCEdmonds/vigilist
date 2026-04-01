from app.services.chunking import chunk_text


def test_short_text_single_chunk():
    text = "Hello world this is a short document."
    chunks = chunk_text(text, chunk_size=500, overlap=50)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_empty_text():
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text(None) == []


def test_chunking_with_overlap():
    words = [f"word{i}" for i in range(100)]
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=30, overlap=5)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert len(chunk.split()) == 30
    first_chunk_words = chunks[0].split()
    second_chunk_words = chunks[1].split()
    assert first_chunk_words[-5:] == second_chunk_words[:5]


def test_chunk_size_boundary():
    words = ["word"] * 500
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=500, overlap=50)
    assert len(chunks) == 1
