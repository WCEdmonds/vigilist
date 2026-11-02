"""Lay out an email as pages so it can be redacted and produced as images.

Emails are ingested without page images (the viewer shows their HTML). To
redact one, it is laid out on US Letter with PyMuPDF's Story (no new
dependency): a fixed header block, then the sanitized HTML body or the plain
text. The PDF has a real text layer, so the page images get word boxes like
any ingested PDF. While laying out page 1 the box of each header line is
recorded, so a redaction over e.g. the Subject line can blank that column in
the load file.

Pure: no DB, no storage, no network (remote resources are stripped first).
"""

from __future__ import annotations

import html as html_lib
import io
from datetime import timezone
from html.parser import HTMLParser
from typing import NamedTuple

import fitz

HEADER_FIELDS = ("from", "to", "cc", "bcc", "date", "subject", "attachments")
MAX_IMAGED_PAGES = 500

_LABELS = {"from": "From:", "to": "To:", "cc": "Cc:", "bcc": "Bcc:", "date": "Date:",
           "subject": "Subject:", "attachments": "Attachments:"}
_PAGE = fitz.paper_rect("letter")
_MARGIN = 54  # points: 0.75 in

# Elements dropped with everything inside them.
_DROP = {"script", "style", "iframe", "object", "embed", "form", "head", "title",
         "noscript", "template", "svg", "video", "audio"}
# Elements dropped on their own (void or harmless wrappers).
_SKIP = {"link", "meta", "base", "input", "button", "select", "textarea"}
_URL_ATTRS = {"src", "href", "background", "srcset", "poster", "action", "formaction"}
_VOID = {"br", "hr", "img", "wbr", "col", "area", "source", "track"}


class EmailPages(NamedTuple):
    pdf: bytes
    header_regions: dict[str, dict]
    page_count: int


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.dropping = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            if tag not in _VOID:
                self.dropping += 1
            return
        if self.dropping or tag in _SKIP:
            return
        kept = []
        for name, value in attrs:
            value = value or ""
            if name.startswith("on"):
                continue
            if name in _URL_ATTRS and not value.strip().lower().startswith("data:"):
                continue
            if name == "style":
                low = value.replace(" ", "").lower()
                if "display:none" in low or "visibility:hidden" in low or "url(" in low:
                    continue
            kept.append(f' {name}="{html_lib.escape(value, quote=True)}"')
        self.out.append(f"<{tag}{''.join(kept)}>")

    def handle_endtag(self, tag):
        if tag in _DROP:
            self.dropping = max(0, self.dropping - 1)
            return
        if self.dropping or tag in _SKIP or tag in _VOID:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.dropping:
            self.out.append(html_lib.escape(data, quote=False))


def sanitize_body_html(html: str) -> str:
    """Strip scripts, embedded objects, hidden content and every non-data:
    URL, so laying the email out never fetches anything or shows less than
    the reviewer could see."""
    parser = _Sanitizer()
    parser.feed(html or "")
    parser.close()
    return "".join(parser.out)


def _date_text(doc) -> str:
    when = doc.date_sent or doc.date_received
    if when is None:
        return ""
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%d %H:%M UTC")


def email_html(doc, attachment_names: list[str]) -> str:
    values = {
        "from": doc.email_from, "to": doc.email_to, "cc": doc.email_cc,
        "bcc": doc.email_bcc, "date": _date_text(doc), "subject": doc.email_subject,
        "attachments": "; ".join(attachment_names),
    }
    rows = []
    for field in HEADER_FIELDS:
        value = (values[field] or "").strip()
        if not value and field != "subject":
            continue
        rows.append(f'<p id="hdr-{field}" style="margin:0 0 3pt 0">'
                    f"<b>{_LABELS[field]}</b> {html_lib.escape(value)}</p>")
    header = ('<div style="font-family:sans-serif;font-size:10pt">'
              + "".join(rows) + "<hr/></div>")
    if doc.email_body_html:
        body = sanitize_body_html(doc.email_body_html)
    elif doc.text_content:
        body = ('<pre style="font-family:sans-serif;font-size:10pt;white-space:pre-wrap">'
                + html_lib.escape(doc.text_content) + "</pre>")
    else:
        body = ""
    return header + '<div style="font-size:10.5pt">' + body + "</div>"


def render_email_pages(doc, attachment_names: list[str]) -> EmailPages:
    """Lay the email out on Letter pages. ValueError past MAX_IMAGED_PAGES."""
    story = fitz.Story(html=email_html(doc, attachment_names))
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    where = _PAGE + (_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)
    regions: dict[str, dict] = {}

    def record(pos):
        ident = getattr(pos, "id", "") or ""
        if pos.page == 1 and ident.startswith("hdr-") and ident[4:] not in regions:
            r = fitz.Rect(pos.rect)
            regions[ident[4:]] = {
                "page": 1,
                "x": round(max(0.0, r.x0 / _PAGE.width * 100), 2),
                "y": round(max(0.0, r.y0 / _PAGE.height * 100), 2),
                "w": round(min(100.0, r.x1 / _PAGE.width * 100) - max(0.0, r.x0 / _PAGE.width * 100), 2),
                "h": round(min(100.0, r.y1 / _PAGE.height * 100) - max(0.0, r.y0 / _PAGE.height * 100), 2),
            }

    pages = 0
    more = True
    while more:
        pages += 1
        if pages > MAX_IMAGED_PAGES:
            writer.close()
            raise ValueError(f"too many pages to image (more than {MAX_IMAGED_PAGES})")
        device = writer.begin_page(_PAGE)
        more, _ = story.place(where)
        story.element_positions(record, {"page": pages})
        story.draw(device)
        writer.end_page()
    writer.close()
    return EmailPages(pdf=buf.getvalue(), header_regions=regions, page_count=pages)
