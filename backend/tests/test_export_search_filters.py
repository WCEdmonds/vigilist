"""The search CSV export must resolve the same set the reviewer is looking at."""

import asyncio
from datetime import date

import app.routers.export as rx
import app.services.search as svc
from tests.fakes import FakeSession, FakeUser


def test_search_csv_applies_email_date_and_filters(monkeypatch):
    seen = {}

    async def fake_accessible(db, user):
        return [1]

    async def fake_search(db, q, **kwargs):
        seen["q"] = q
        seen.update(kwargs)
        return [], 0

    monkeypatch.setattr(rx, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(svc, "search_documents", fake_search)

    asyncio.run(rx.export_search_csv(
        q="", production_id=1, sort="date_asc", metadata='{"Custodian": "Doe"}',
        file_type="email", source_party=None, source_type="received",
        email_address="@lawfirm.com", email_scope="any",
        date_from=date(2018, 3, 1), date_to=date(2018, 3, 31), tz="America/New_York",
        db=FakeSession(), user=FakeUser(),
    ))
    assert seen["q"] == ""
    assert seen["sort"] == "date_asc"
    assert seen["metadata_filters"] == {"Custodian": "Doe"}
    assert seen["file_type"] == "email" and seen["source_type"] == "received"
    assert seen["email_address"] == "@lawfirm.com" and seen["email_scope"] == "any"
    assert seen["date_from"] == date(2018, 3, 1) and seen["date_to"] == date(2018, 3, 31)
    assert seen["tz"] == "America/New_York"
