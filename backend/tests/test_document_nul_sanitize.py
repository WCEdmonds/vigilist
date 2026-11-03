"""Postgres rejects NUL (\x00) in text and jsonb; Documents must be sanitized."""
from app.models import Document
from app.services import ingest as ingest_mod
from app.services.email_parse import ParsedMessage
from app.services.ingest_native import build_email_documents

NUL = "\x00"


def _has_nul(obj) -> bool:
    if isinstance(obj, str):
        return NUL in obj
    if isinstance(obj, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_has_nul(v) for v in obj)
    return False


def _doc_has_nul(doc) -> bool:
    return any(_has_nul(getattr(doc, c.key, None)) for c in Document.__table__.columns)


def test_nuls_stripped_from_columns_and_nested_metadata():
    doc = Document(
        production_id=1, bates_begin="ACME 1", bates_end="ACME 1",
        title="Re:\x00 Budget", text_content="hello\x00 world", email_subject="Sub\x00ject",
        email_body_html="<p>a\x00b</p>", file_name="re\x00port.pdf", page_count=3,
        metadata_={"Fi\x00le": "a\x00b", "nested": {"k\x00": ["x\x00", 5, None]}, "n": 7},
    )
    ingest_mod._clamp_string_columns(doc)
    assert not _doc_has_nul(doc)
    assert doc.title == "Re: Budget"
    assert doc.text_content == "hello world"
    assert doc.file_name == "report.pdf"
    assert doc.page_count == 3
    assert doc.metadata_ == {"File": "ab", "nested": {"k": ["x", 5, None]}, "n": 7}


def test_clamp_applies_after_stripping():
    doc = Document(
        production_id=1, bates_begin="ACME 1", bates_end="ACME 1",
        file_name=("a" + NUL) * 400,  # 800 raw chars, 400 after strip: fits String(500)
        email_from=("b" + NUL) * 400 + "c" * 300,  # 700 after strip: overflows
    )
    ingest_mod._clamp_string_columns(doc)
    assert doc.file_name == "a" * 400
    assert len(doc.email_from) == 500
    assert not _doc_has_nul(doc)


def test_build_email_documents_then_sanitize():
    parsed = ParsedMessage(
        from_="a@acme.example.test", to="b@acme.example.test",
        subject="Q3\x00 plan", body_text="body\x00 text", body_html="<p>x\x00</p>",
        attachments=[("not\x00es.txt", b"plain text")],
        message_id="<1\x00@acme.example.test>",
    )
    docs = build_email_documents(
        parsed, "ACME 000001", 1, "inbox\x00/a.msg", "Custodian\x00", b"raw",
        extract_fn=lambda name, data, ocr_fn=None: type(
            "R", (), {"text": "att\x00text", "file_type": "txt",
                      "extraction_status": "complete", "extraction_error": None})(),
    )
    assert any(_doc_has_nul(d) for d in docs)  # precondition: NULs do get through
    for d in docs:
        ingest_mod._clamp_string_columns(d)
    assert not any(_doc_has_nul(d) for d in docs)
    assert docs[0].email_subject == "Q3 plan"
