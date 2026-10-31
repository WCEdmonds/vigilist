"""Email metadata report: the columns a reviewer hands to a partner or
opposing counsel (Bates, family, date, from, to, cc, subject)."""

import asyncio
import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import app.routers.export as rx
from app.services.metadata_report import METADATA_HEADERS, metadata_row
from tests.fakes import FakeResult, FakeSession, FakeUser

SENT = datetime(2026, 3, 2, 3, 30, tzinfo=timezone.utc)


def _doc(**kw):
    base = dict(
        id=uuid4(), production_id=4,
        bates_begin="ACME 000004 -0776", bates_end="ACME 000004 -0776",
        family_id="ACME 000004 -0776", date_sent=SENT, date_received=None,
        email_from="alice@acme.example", email_to="bob@acme.example",
        email_cc="carol@acme.example", email_bcc=None,
        email_subject="RE: Q3 planning", title="Q3 planning", custodian="A. Smith",
        file_type="email", page_count=2, tags=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tag(category, name):
    return SimpleNamespace(tag=SimpleNamespace(category=category, name=name))


def _row(doc, tz=None):
    return dict(zip(METADATA_HEADERS, metadata_row(doc, tz)))


def test_email_row_has_the_requested_columns():
    r = _row(_doc(tags=[_tag("responsiveness", "Responsive"), _tag("privilege", "Attorney-Client")]))
    assert r["Bates Begin"] == "ACME 000004 -0776"
    assert r["Family"] == "ACME 000004 -0776"
    assert r["Parent Bates"] == ""          # the email is the parent
    assert r["Date Sent"] == "2026-03-02 03:30"
    assert r["From"] == "alice@acme.example"
    assert r["To"] == "bob@acme.example"
    assert r["CC"] == "carol@acme.example"
    assert r["BCC"] == ""
    assert r["Subject"] == "RE: Q3 planning"
    assert r["Tags"] == "Attorney-Client; Responsive"


def test_attachment_row_names_its_parent():
    r = _row(_doc(bates_begin="ACME 000004 -0776 .0001", bates_end="ACME 000004 -0776 .0001",
                  email_subject=None, title="budget.xlsx", file_type="xlsx",
                  email_from=None, email_to=None, email_cc=None))
    assert r["Parent Bates"] == "ACME 000004 -0776"
    assert r["Subject"] == ""
    assert r["Title"] == "budget.xlsx"


def test_date_is_in_the_reviewers_timezone():
    # 03:30 UTC on Mar 2 is still Mar 1 in New York.
    assert _row(_doc(), tz="America/New_York")["Date Sent"] == "2026-03-01 22:30"


def test_unknown_timezone_falls_back_to_utc():
    assert _row(_doc(), tz="Not/AZone")["Date Sent"] == "2026-03-02 03:30"


def test_received_date_used_when_no_sent_date():
    assert _row(_doc(date_sent=None, date_received=SENT))["Date Sent"] == "2026-03-02 03:30"
    assert _row(_doc(date_sent=None))["Date Sent"] == ""


def test_search_csv_writes_metadata_in_result_order(monkeypatch):
    first, second = _doc(bates_begin="ACME 2"), _doc(bates_begin="ACME 1", email_subject="Hi")

    async def fake_search(db, q, **kw):
        return ([{"id": first.id, "snippet": "budget <mark>talk</mark>"},
                 {"id": second.id, "snippet": ""}], 2)

    async def fake_accessible(db, user):
        return [4]

    import app.services.search as search_svc
    monkeypatch.setattr(search_svc, "search_documents", fake_search)
    monkeypatch.setattr(rx, "get_accessible_production_ids", fake_accessible)
    # DB returns docs in a different order than the search ranked them.
    db = FakeSession(responders=[("FROM documents", FakeResult(items=[second, first]))])

    resp = asyncio.run(rx.export_search_csv(
        q="budget", production_id=4, sort="relevance", metadata=None, file_type=None,
        source_party=None, source_type=None, email_address=None, email_scope="from",
        date_from=None, date_to=None, tz=None, db=db, user=FakeUser(),
    ))
    body = b"".join(asyncio.run(_drain(resp.body_iterator))).decode("utf-8")
    assert body.startswith("﻿")        # Excel reads UTF-8 names correctly
    rows = list(csv.DictReader(io.StringIO(body.lstrip("﻿"))))
    assert [r["Bates Begin"] for r in rows] == ["ACME 2", "ACME 1"]
    assert rows[0]["Snippet"] == "budget talk"   # highlight markup stripped
    assert rows[1]["Subject"] == "Hi"
    assert set(METADATA_HEADERS) <= set(rows[0])


async def _drain(it):
    return [c.encode() if isinstance(c, str) else c async for c in _aiter(it)]


async def _aiter(it):
    if hasattr(it, "__aiter__"):
        async for c in it:
            yield c
    else:
        for c in it:
            yield c
