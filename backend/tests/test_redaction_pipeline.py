"""End to end, with real PyMuPDF and Pillow: an email is imaged, redacted
over its subject line and an account number, and every produced form —
burned page, produced text, load-file fields — has nothing under the boxes."""

import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image

import app.services.imaging as im
import app.services.ingest_pdf as ip
from app.services.redacted_metadata import blanked_fields
from app.services.redacted_text import REDACTED, produced_text
from app.services.redaction_render import burn_page


def _box(page, x, y, w, h):
    return SimpleNamespace(page_num=page, x_pct=x, y_pct=y, w_pct=w, h_pct=h, reason_code="pii")


def test_email_redaction_pipeline(monkeypatch):
    stored = {}
    monkeypatch.setattr(ip, "upload_bytes", lambda data, path, content_type=None: stored.__setitem__(path, data))
    doc = SimpleNamespace(
        id=uuid4(), production_id=4, bates_begin="ACME 000004 -0007", family_id="ACME 000004 -0007",
        file_type="email", native_path="productions/4/raw/box.pst", image_paths=[], ocr_paths=[],
        email_from="alice@acme.example", email_to="bob@acme.example", email_cc=None, email_bcc=None,
        email_subject="RE: ACME settlement", date_sent=datetime(2026, 3, 2, 3, 30, tzinfo=timezone.utc),
        date_received=None, email_body_html="<p>Please wire to account 4417-1234 by Friday.</p>",
        text_content=None)

    images, ocrs, count, regions = im.build_pages(doc, "email", [], ocr_fn=lambda jpeg: None)
    sidecar = json.loads(stored[ocrs[0]])
    acct = next(w for w in sidecar["words"] if w["t"].startswith("4417"))
    subj = regions["subject"]
    reds = [_box(1, subj["x"], subj["y"], subj["w"], subj["h"]),
            _box(1, acct["x"], acct["y"], acct["w"], acct["h"])]

    page = burn_page(Image.open(io.BytesIO(stored[images[0]])), reds)
    cx = int((acct["x"] + acct["w"] / 2) / 100 * page.width)
    cy = int((acct["y"] + acct["h"] / 2) / 100 * page.height)
    assert page.convert("L").getpixel((cx, cy)) < 40

    text = produced_text([sidecar], {1: reds})
    assert REDACTED in text and "Please wire to account" in text
    assert "4417" not in text and "settlement" not in text

    assert blanked_fields(regions, reds) == {"subject"}
