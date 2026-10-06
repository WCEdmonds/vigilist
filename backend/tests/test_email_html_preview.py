"""Email preview: the original HTML body is captured at parse time and kept
off the hot select path."""

from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import Document
from app.services.email_parse import _parse_eml_bytes
from app.services.ingest_native import build_email_documents

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _html_email() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Will <will@votethiru.com>"
    msg["To"] = "press@votethiru.com"
    msg["Subject"] = "Draft announcement"
    msg.set_content("plain body")
    msg.add_alternative('<p>Hello <b>team</b></p><img src="cid:logo123">', subtype="html")
    html_part = msg.get_payload()[1]
    html_part.add_related(PNG, maintype="image", subtype="png", cid="<logo123>")
    return msg.as_bytes()


def test_parse_keeps_html_and_inlines_cid_images():
    parsed = _parse_eml_bytes(_html_email())
    assert "<b>team</b>" in parsed.body_html
    assert "cid:logo123" not in parsed.body_html
    assert 'src="data:image/png;base64,' in parsed.body_html
    assert parsed.body_text == "plain body"


def test_plain_text_email_has_no_html():
    msg = EmailMessage()
    msg["From"] = "a@b.c"
    msg.set_content("just text")
    assert _parse_eml_bytes(msg.as_bytes()).body_html == ""


def test_email_document_carries_html_body():
    parsed = _parse_eml_bytes(_html_email())
    docs = build_email_documents(
        parsed, "VT 000001", 9, "will-part01.mbox", "will@votethiru.com", b"raw",
        extract_fn=lambda *a, **k: None,
    )
    assert "<b>team</b>" in docs[0].email_body_html


def test_email_body_html_is_deferred():
    sql = str(select(Document).compile(dialect=postgresql.dialect()))
    assert "email_body_html" not in sql
