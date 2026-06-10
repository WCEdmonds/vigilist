from app.services.ingest_pdf import derive_bates_prefix


def test_prefix_uppercases_first_token():
    assert derive_bates_prefix("Smith Loose Docs") == "SMITH"


def test_prefix_strips_punctuation():
    assert derive_bates_prefix("smith-jones, llp") == "SMITHJONES"


def test_prefix_truncates_to_12_chars():
    assert derive_bates_prefix("Supercalifragilistic Matter") == "SUPERCALIFRA"


def test_prefix_falls_back_to_doc_when_empty():
    assert derive_bates_prefix("!!! ???") == "DOC"
    assert derive_bates_prefix("") == "DOC"
