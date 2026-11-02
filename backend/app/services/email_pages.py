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
import logging
import re
import unicodedata
from collections import Counter
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
_WHERE = _PAGE + (_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)
logger = logging.getLogger(__name__)

# Elements dropped with everything inside them.
_DROP = {"script", "style", "iframe", "object", "embed", "form", "head", "title",
         "noscript", "template", "svg", "video", "audio"}
# Elements dropped on their own (void or harmless wrappers).
_SKIP = {"link", "meta", "base", "input", "button", "select", "textarea"}
_URL_ATTRS = {"src", "href", "background", "srcset", "poster", "action", "formaction"}
_VOID = {"br", "hr", "img", "wbr", "col", "area", "source", "track"}


class EmailPages(NamedTuple):
    pdf: bytes
    # field -> one box per page the header line is on (a long To list can
    # push later fields onto page 2).
    header_regions: dict[str, list[dict]]
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


# No font-size anywhere: MuPDF's Story silently drops the overflow of a block
# taller than a page whenever a non-default font size applies to it (inline
# style or CSS alike) — text renders at the default 12pt instead.
_CSS = "body{font-family:sans-serif} p{margin:0 0 3pt 0}"


def _header_values(doc, attachment_names: list[str]) -> dict[str, str]:
    return {
        "from": doc.email_from, "to": doc.email_to, "cc": doc.email_cc,
        "bcc": doc.email_bcc, "date": _date_text(doc), "subject": doc.email_subject,
        "attachments": "; ".join(attachment_names),
    }


def _plain_paragraphs(text: str) -> str:
    """One top-level <p> per line: MuPDF's Story drops the part of a single
    block that doesn't fit on a page when it is nested (in a <div>/<pre>)."""
    out = []
    for line in (text or "").splitlines():
        lead = len(line) - len(line.lstrip(" "))
        out.append(f'<p style="margin:0">{"&nbsp;" * lead}{html_lib.escape(line.lstrip(" ")) or "&nbsp;"}</p>')
    return "".join(out)


def email_html(doc, attachment_names: list[str], plain: bool = False) -> str:
    """Header block (top-level paragraphs, no wrapper), a marker, then the
    body: sanitized HTML, or as plain paragraphs (the HTML's own text when
    there is HTML, else text_content) when plain=True or there is no HTML."""
    values = _header_values(doc, attachment_names)
    rows = []
    for field in HEADER_FIELDS:
        value = (values[field] or "").strip()
        if not value and field != "subject":
            continue
        rows.append(f'<p id="hdr-{field}">'
                    f"<b>{_LABELS[field]}</b> {html_lib.escape(value)}</p>")
    rows.append('<hr id="hdr-end"/>')
    if doc.email_body_html and not plain:
        body = sanitize_body_html(doc.email_body_html)
    else:
        body = _plain_paragraphs(_plain_body(doc))
    return "".join(rows) + body


def _plain_body(doc) -> str:
    # The fallback shows the same words the reviewer saw in the HTML view.
    return _html_text(doc.email_body_html) if doc.email_body_html else (doc.text_content or "")


_BLOCKS = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6",
           "blockquote", "pre", "hr", "ul", "ol"}


NEWLINE = chr(10)


class _TextOf(HTMLParser):
    """The text of (already sanitized) HTML, with a line break per block."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCKS:
            self.parts.append(NEWLINE)

    def handle_endtag(self, tag):
        if tag in _BLOCKS:
            self.parts.append(NEWLINE)

    def handle_data(self, data):
        self.parts.append(data)


def _html_text(html: str) -> str:
    parser = _TextOf()
    parser.feed(sanitize_body_html(html))
    parser.close()
    return "".join(parser.parts)


def _tokens(text: str) -> Counter:
    text = unicodedata.normalize("NFKC", text.replace("­", "")).lower()
    return Counter(re.findall(r"\w+", text))


def _missing_words(expected: str, pdf: bytes) -> int:
    got = _tokens("".join(page.get_text() for page in fitz.open(stream=pdf, filetype="pdf")))
    want = _tokens(expected)
    return sum(max(0, n - got[t]) for t, n in want.items())


def _layout(html: str):
    """Lay html out on Letter pages: (pdf, page_count, starts) where starts
    maps each hdr-* id to the (page, y in points) its element begins at."""
    story = fitz.Story(html=html, user_css=_CSS)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    starts: dict[str, tuple[int, float]] = {}

    def record(pos):
        ident = getattr(pos, "id", "") or ""
        if ident.startswith("hdr-") and ident not in starts:
            starts[ident] = (pos.page, fitz.Rect(pos.rect).y0)

    pages = 0
    more = True
    while more:
        pages += 1
        if pages > MAX_IMAGED_PAGES:
            writer.close()
            raise ValueError(f"too many pages to image (more than {MAX_IMAGED_PAGES})")
        device = writer.begin_page(_PAGE)
        more, _ = story.place(_WHERE)
        story.element_positions(record, {"page": pages})
        story.draw(device)
        writer.end_page()
    writer.close()
    return buf.getvalue(), pages, starts


def _regions(starts: dict[str, tuple[int, float]]) -> dict[str, list[dict]]:
    """Each header line runs from its own start to the next header element's
    start (the last one to the hdr-end marker), split into one box per page;
    a long To list can span several pages."""
    order = sorted(starts.items(), key=lambda kv: kv[1])
    x = round(_WHERE.x0 / _PAGE.width * 100, 2)
    w = round(_WHERE.width / _PAGE.width * 100, 2)
    pct = lambda pt: round(pt / _PAGE.height * 100, 2)  # noqa: E731
    out: dict[str, list[dict]] = {}
    for (ident, (page, y)), (_next, (npage, ny)) in zip(order, order[1:]):
        if not ident.startswith("hdr-") or ident == "hdr-end":
            continue
        boxes = []
        for p in range(page, npage + 1):
            top = y if p == page else _WHERE.y0
            bottom = ny if p == npage else _WHERE.y1
            if bottom > top:
                boxes.append({"page": p, "x": x, "y": pct(top), "w": w, "h": pct(bottom) - pct(top)})
        out[ident[4:]] = boxes
    return out


def render_email_pages(doc, attachment_names: list[str]) -> EmailPages:
    """Lay the email out on Letter pages and prove nothing was dropped: every
    word must reach the PDF. An HTML body that doesn't lay out completely is
    laid out again as plain text; if that still loses words, ValueError
    (never a silently partial email). ValueError past MAX_IMAGED_PAGES too."""
    header_text = " ".join(_LABELS[f] + " " + (v or "")
                           for f, v in _header_values(doc, attachment_names).items()
                           if (v or "").strip() or f == "subject")
    attempts = [False, True] if doc.email_body_html else [True]
    for plain in attempts:
        html = email_html(doc, attachment_names, plain=plain)
        body_text = _plain_body(doc) if plain else _html_text(doc.email_body_html)
        try:
            pdf, pages, starts = _layout(html)
        except ValueError:
            # A block Story can't split repeats onto page after page; the
            # plain-text layout has no such blocks. Genuinely huge stays an error.
            if plain:
                raise
            logger.warning("HTML layout ran past %d pages; retrying as plain text", MAX_IMAGED_PAGES)
            continue
        missing = _missing_words(header_text + " " + body_text, pdf)
        if missing == 0:
            return EmailPages(pdf=pdf, header_regions=_regions(starts), page_count=pages)
        logger.warning("Email layout dropped %d words (plain=%s); %s", missing, plain,
                       "retrying as plain text" if not plain else "giving up")
    raise ValueError(f"could not lay out the whole email ({missing} words would be missing)")
