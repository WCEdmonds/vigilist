"""Emails laid out as pages so they can be redacted and produced as images."""

from datetime import datetime, timezone
from types import SimpleNamespace

import fitz
import pytest

import app.services.email_pages as ep

SENT = datetime(2026, 3, 2, 3, 30, tzinfo=timezone.utc)


def _email(**kw):
    base = dict(email_from="Alice <alice@acme.example>", email_to="bob@acme.example",
                email_cc=None, email_bcc=None, email_subject="RE: ACME settlement",
                date_sent=SENT, date_received=None, email_body_html=None,
                text_content="Short body.")
    base.update(kw)
    return SimpleNamespace(**base)


def test_header_regions_cover_each_present_field():
    pages = ep.render_email_pages(_email(), ["budget.xlsx", "notes.pdf"])
    regions = pages.header_regions
    assert set(regions) == {"from", "to", "date", "subject", "attachments"}
    for r in regions.values():
        assert r["page"] == 1
        assert r["x"] >= 0 and r["y"] >= 0
        assert r["x"] + r["w"] <= 100 and r["y"] + r["h"] <= 100
    assert regions["subject"]["y"] > regions["from"]["y"]


def test_long_body_spans_pages_and_has_text_layer():
    body = "".join(f"<p>Paragraph {i} about the ACME budget.</p>" for i in range(200))
    pages = ep.render_email_pages(_email(email_body_html=body), [])
    assert pages.page_count > 1
    pdf = fitz.open(stream=pages.pdf, filetype="pdf")
    assert pdf.page_count == pages.page_count
    text = pdf[0].get_text()
    assert "Subject:" in text and "RE: ACME settlement" in text
    assert "2026-03-02 03:30 UTC" in text


def test_headers_only_email_is_one_page():
    pages = ep.render_email_pages(_email(text_content=None, email_body_html=None), [])
    assert pages.page_count == 1
    assert "subject" in pages.header_regions


def test_remote_resources_and_scripts_are_stripped():
    html = ('<img src="http://evil.example/x.png"><script>x()</script>'
            '<a href="https://evil.example/">link</a>'
            '<img src="data:image/png;base64,iVBORw0KGgo=">')
    out = ep.sanitize_body_html(html)
    assert "evil.example" not in out
    assert "<script" not in out
    assert "data:image/png;base64,iVBORw0KGgo=" in out


def test_page_cap(monkeypatch):
    monkeypatch.setattr(ep, "MAX_IMAGED_PAGES", 2)
    body = "".join(f"<p>Paragraph {i}</p>" for i in range(400))
    with pytest.raises(ValueError, match="too many pages"):
        ep.render_email_pages(_email(email_body_html=body), [])


def test_non_ascii_subject_survives():
    pages = ep.render_email_pages(_email(email_subject="Café – Q3"), [])
    assert "Café – Q3" in fitz.open(stream=pages.pdf, filetype="pdf")[0].get_text()
