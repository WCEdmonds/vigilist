"""Bulk tag-by-query service: pure planner + DB helpers on a fake session."""

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import app.services.bulk_tagging as bt
from app.models import Document
from app.schemas import BulkTagByQueryRequest, BulkTagQuery
from tests.fakes import FakeResult, FakeSession

A, B, C, D = (uuid4() for _ in range(4))


class FakeTag:
    def __init__(self, tag_id=5, category="responsiveness", name="Not Responsive"):
        self.id = tag_id
        self.category = category
        self.name = name


# ── plan_tagging ──

def test_plan_no_conflicts_tags_untagged_targets():
    plan = bt.plan_tagging({A, B, C}, already_tagged={A}, conflicts={}, conflict_action=None)
    assert plan.to_tag == {B, C}
    assert plan.already_tagged == {A}
    assert plan.conflicts == {} and plan.switch == {} and plan.left == frozenset()
    assert plan.coded == {A, B, C}


def test_plan_switch_retags_conflicting_docs():
    plan = bt.plan_tagging({A, B}, set(), conflicts={B: {9}}, conflict_action="switch")
    assert plan.to_tag == {A, B}
    assert plan.switch == {B: {9}}
    assert plan.left == frozenset()


def test_plan_leave_skips_conflicting_docs():
    plan = bt.plan_tagging({A, B}, set(), conflicts={B: {9}}, conflict_action="leave")
    assert plan.to_tag == {A}
    assert plan.left == {B}
    assert plan.switch == {}
    assert plan.coded == {A}


def test_plan_unresolved_reports_conflicts_without_switching():
    plan = bt.plan_tagging({A, B}, set(), conflicts={B: {9}}, conflict_action=None)
    assert plan.conflicts == {B: {9}}
    assert plan.switch == {} and plan.left == frozenset()


def test_plan_already_tagged_doc_is_not_a_conflict():
    plan = bt.plan_tagging({A}, already_tagged={A}, conflicts={A: {9}}, conflict_action="switch")
    assert plan.conflicts == {}
    assert plan.switch == {}
    assert plan.to_tag == frozenset()


def test_plan_ignores_conflicts_outside_targets():
    plan = bt.plan_tagging({A}, set(), conflicts={D: {9}}, conflict_action=None)
    assert plan.conflicts == {}


def test_conflict_tag_counts_sorted_by_count():
    counts = bt.conflict_tag_counts({A: {1}, B: {1, 2}, C: {1}}, {1: "Responsive", 2: "Needs Review"})
    assert counts == [
        {"tag_id": 1, "name": "Responsive", "count": 3},
        {"tag_id": 2, "name": "Needs Review", "count": 1},
    ]


# ── SQL shape ──

def test_ids_match_is_single_array_bind():
    ids = [uuid4() for _ in range(40_000)]
    compiled = select(Document.id).where(bt.ids_match(Document.id, ids)).compile(dialect=postgresql.dialect())
    assert "= ANY (" in str(compiled)
    assert len(compiled.params) == 1


# ── DB helpers ──

def test_expand_attachments_merges_family_rows():
    db = FakeSession(responders=[("FROM documents", FakeResult(rows=[(A, 1), (C, 1)]))])
    out = asyncio.run(bt.expand_attachments(db, {A: 1}))
    assert out == {A: 1, C: 1}


def test_expand_attachments_joins_on_production():
    db = FakeSession()
    asyncio.run(bt.expand_attachments(db, {A: 1}))
    sql = db.executed[0]
    assert "family_id = documents.family_id" in sql or "documents.family_id = documents_1.family_id" in sql
    assert "production_id = documents.production_id" in sql or "documents.production_id = documents_1.production_id" in sql
    assert "family_id IS NOT NULL" in sql


def test_expand_attachments_empty_input_skips_query():
    db = FakeSession()
    assert asyncio.run(bt.expand_attachments(db, {})) == {}
    assert db.executed == []


def test_conflicting_tags_only_for_exclusive_categories():
    db = FakeSession()
    out = asyncio.run(bt.conflicting_tags(db, FakeTag(category="privilege"), [A]))
    assert out == ({}, {})
    assert db.executed == []


def test_conflicting_tags_groups_rows_by_doc():
    rows = [(A, 1, "Responsive"), (A, 3, "Needs Review"), (B, 1, "Responsive")]
    db = FakeSession(responders=[("FROM document_tags", FakeResult(rows=rows))])
    by_doc, names = asyncio.run(bt.conflicting_tags(db, FakeTag(), [A, B]))
    assert by_doc == {A: {1, 3}, B: {1}}
    assert names == {1: "Responsive", 3: "Needs Review"}


def test_resolve_query_docs_rejects_empty_criteria():
    with pytest.raises(bt.NoCriteriaError):
        asyncio.run(bt.resolve_query_docs(FakeSession(), BulkTagQuery(), [1]))


def test_resolve_query_docs_returns_id_to_production():
    db = FakeSession(responders=[("FROM documents", FakeResult(rows=[(A, 1), (B, 2)]))])
    out = asyncio.run(bt.resolve_query_docs(db, BulkTagQuery(email_address="jane@x.com"), [1, 2]))
    assert out == {A: 1, B: 2}
    # Generic-dialect str(): ILIKE renders as lower(..) LIKE lower(..); the
    # Postgres ILIKE form is pinned in test_search_email_filter.py.
    assert "documents.email_from" in db.executed[0] and "LIKE" in db.executed[0]
    assert "LIMIT" in db.executed[0]


def test_resolve_query_docs_honours_date_range():
    from datetime import date
    db = FakeSession(responders=[("FROM documents", FakeResult(rows=[(A, 1)]))])
    asyncio.run(bt.resolve_query_docs(db, BulkTagQuery(date_from=date(2018, 3, 1)), [1]))
    assert "coalesce(documents.date_sent, documents.date_created) >=" in db.executed[0]


def test_apply_plan_switch_deletes_then_inserts():
    plan = bt.plan_tagging({A, B}, set(), conflicts={B: {9}}, conflict_action="switch")
    db = FakeSession()
    overridden = asyncio.run(bt.apply_plan(db, plan, tag_id=5, user_id="u1", override={}))
    assert overridden == 0
    assert db.executed[0].startswith("DELETE FROM document_tags")
    assert "= ANY (" in db.executed[0]
    assert db.executed[1].startswith("INSERT INTO document_tags")
    assert "ON CONFLICT" in db.executed[1]
    rows = db.executed_params[1]
    assert {r["document_id"] for r in rows} == {A, B}
    assert all(r["tag_id"] == 5 and r["applied_by"] == "u1" for r in rows)
    assert not any(s.startswith("UPDATE") for s in db.executed)


def test_apply_plan_override_updates_coded_docs():
    plan = bt.plan_tagging({A, B}, already_tagged={A}, conflicts={}, conflict_action=None)
    db = FakeSession()
    overridden = asyncio.run(bt.apply_plan(
        db, plan, tag_id=5, user_id="u1",
        override={"privilege_disposition": "withhold", "privilege_description": "Counsel comms"}))
    assert overridden == 2
    update_sql = next(s for s in db.executed if s.startswith("UPDATE documents"))
    assert "privilege_disposition" in update_sql and "privilege_description" in update_sql
    assert "= ANY (" in update_sql


def test_apply_plan_nothing_to_do_executes_nothing():
    plan = bt.plan_tagging({A}, already_tagged={A}, conflicts={}, conflict_action=None)
    db = FakeSession()
    asyncio.run(bt.apply_plan(db, plan, tag_id=5, user_id="u1", override={}))
    assert db.executed == []


# ── request schema ──

def test_request_requires_exactly_one_target():
    with pytest.raises(ValidationError):
        BulkTagByQueryRequest(tag_id=1)
    with pytest.raises(ValidationError):
        BulkTagByQueryRequest(tag_id=1, doc_ids=[A], query=BulkTagQuery(q="x"))
    with pytest.raises(ValidationError):
        BulkTagByQueryRequest(tag_id=1, doc_ids=[])
    assert BulkTagByQueryRequest(tag_id=1, doc_ids=[A]).include_attachments is True


def test_request_rejects_unknown_conflict_action():
    with pytest.raises(ValidationError):
        BulkTagByQueryRequest(tag_id=1, doc_ids=[A], conflict_action="merge")
