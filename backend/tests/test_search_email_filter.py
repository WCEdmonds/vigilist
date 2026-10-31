"""Email participant + date filters and the shared search-condition builder.
No DB: conditions are compiled with the Postgres dialect; bind values are read
from compiled.params so escaping/bounds are checked on the exact value sent."""

import asyncio
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

import app.routers.search as rs
from app.services.search import (
    build_search_conditions, date_order, email_address_condition, escape_like,
)
from tests.fakes import FakeSession, FakeUser

DOC_DATE = "coalesce(documents.date_sent, documents.date_created)"


def _compile(clause):
    compiled = clause.compile(dialect=postgresql.dialect())
    return str(compiled), list(compiled.params.values())


def test_escape_like_makes_metacharacters_literal():
    assert escape_like("a_b%c\\d") == "a\\_b\\%c\\\\d"
    assert escape_like("jane@lawfirm.com") == "jane@lawfirm.com"


def test_sender_scope_matches_from_only():
    sql, params = _compile(email_address_condition("Jane@LawFirm.com", "from"))
    assert "documents.email_from ILIKE" in sql
    assert "email_to" not in sql and "email_cc" not in sql and "email_bcc" not in sql
    assert params == ["%Jane@LawFirm.com%"]


def test_any_scope_matches_all_participant_columns():
    sql, params = _compile(email_address_condition("@lawfirm.com", "any"))
    for col in ("email_from", "email_to", "email_cc", "email_bcc"):
        assert f"documents.{col} ILIKE" in sql
    assert " OR " in sql
    assert params == ["%@lawfirm.com%"] * 4


def test_metacharacters_escaped_in_bind_value():
    sql, params = _compile(email_address_condition("j_smith@firm.com", "from"))
    assert "ESCAPE" in sql
    assert params == ["%j\\_smith@firm.com%"]


def test_address_is_trimmed():
    _, params = _compile(email_address_condition("  jane@x.com  ", "from"))
    assert params == ["%jane@x.com%"]


def test_email_alone_is_enough_criteria():
    conditions, tsquery, has_criteria = build_search_conditions("", email_address="jane@x.com")
    assert has_criteria is True
    assert tsquery is None
    assert len(conditions) == 1


def test_blank_email_is_ignored():
    conditions, tsquery, has_criteria = build_search_conditions("", email_address="   ")
    assert has_criteria is False
    assert conditions == []


def test_email_combines_with_text_and_scope():
    conditions, tsquery, has_criteria = build_search_conditions(
        "settlement", production_id=12, accessible_production_ids=[12],
        email_address="jane@x.com", email_scope="any",
    )
    assert has_criteria is True
    assert tsquery is not None
    # accessible + production + text + email
    assert len(conditions) == 4


def test_invalid_metadata_key_still_rejected():
    with pytest.raises(HTTPException) as exc:
        build_search_conditions("", metadata_filters={"bad;key": "x"})
    assert exc.value.status_code == 400


def test_date_range_is_inclusive_whole_days():
    conditions, _, has_criteria = build_search_conditions(
        "", date_from=date(2018, 3, 1), date_to=date(2018, 3, 31))
    assert has_criteria is True
    assert len(conditions) == 2
    lo_sql, lo_params = _compile(conditions[0])
    hi_sql, hi_params = _compile(conditions[1])
    assert f"{DOC_DATE} >=" in lo_sql
    assert lo_params == [datetime(2018, 3, 1, tzinfo=timezone.utc)]
    # "to" is inclusive: everything before the start of the NEXT day.
    assert f"{DOC_DATE} <" in hi_sql
    assert hi_params == [datetime(2018, 4, 1, tzinfo=timezone.utc)]


def test_date_bound_alone_is_criteria():
    _, _, has_criteria = build_search_conditions("", date_to=date(2018, 1, 1))
    assert has_criteria is True


def test_date_order_puts_undated_last():
    desc = str(date_order("desc")[0].compile(dialect=postgresql.dialect()))
    asc = str(date_order("asc")[0].compile(dialect=postgresql.dialect()))
    assert desc == f"{DOC_DATE} DESC NULLS LAST"
    assert asc == f"{DOC_DATE} ASC NULLS LAST"


# ── router ──

def _patch_router(monkeypatch):
    calls = {}

    async def fake_accessible(db, user):
        return [1]

    async def fake_semantic(*args, **kwargs):
        calls["semantic"] = True
        return [], 0

    async def fake_search(db, q, **kwargs):
        calls["search"] = kwargs
        return [], 0

    async def fake_log(*args, **kwargs):
        pass

    monkeypatch.setattr(rs, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rs, "semantic_search", fake_semantic)
    monkeypatch.setattr(rs, "search_documents", fake_search)
    monkeypatch.setattr(rs, "log_action", fake_log)
    return calls


def _call_search(**overrides):
    # Router functions called directly get Query() objects as defaults, so
    # every parameter is passed explicitly.
    params = dict(
        q="", production_id=1, page=1, per_page=50, sort="relevance", mode="fulltext",
        metadata=None, file_type=None, source_party=None, source_type=None,
        email_address=None, email_scope="from", date_from=None, date_to=None,
    )
    params.update(overrides)
    return asyncio.run(rs.search(**params, db=FakeSession(), user=FakeUser()))


def test_search_router_passes_email_and_skips_semantic(monkeypatch):
    calls = _patch_router(monkeypatch)
    _call_search(q="what did jane say", mode="semantic",
                 email_address="jane@x.com", email_scope="any")
    assert "semantic" not in calls
    assert calls["search"]["email_address"] == "jane@x.com"
    assert calls["search"]["email_scope"] == "any"


def test_search_router_passes_dates_and_date_sort(monkeypatch):
    calls = _patch_router(monkeypatch)
    _call_search(q="what did jane say", mode="semantic", sort="date_desc",
                 date_from=date(2018, 3, 1))
    assert "semantic" not in calls
    assert calls["search"]["date_from"] == date(2018, 3, 1)
    assert calls["search"]["sort"] == "date_desc"


def test_search_router_rejects_reversed_dates(monkeypatch):
    _patch_router(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _call_search(date_from=date(2018, 4, 1), date_to=date(2018, 3, 1))
    assert exc.value.status_code == 422
