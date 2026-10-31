"""Attachments must say what they're attached to (customer: they "shouldn't
just show up in the full list like a normal email")."""

import asyncio
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.services.families import is_attachment, parent_refs
from tests.fakes import FakeResult, FakeSession

PARENT = uuid4()
ATT = uuid4()
LOOSE = uuid4()


def test_is_attachment_uses_family_vs_own_number():
    assert is_attachment("ACME 000004 -0776 .0001", "ACME 000004 -0776")
    assert not is_attachment("ACME 000004 -0776", "ACME 000004 -0776")   # the email itself
    assert not is_attachment("ACME 000009", None)                         # loose file
    assert not is_attachment("ACME 000009", "")


def test_parent_refs_resolves_parent_in_same_production():
    db = FakeSession(responders=[("FROM documents", FakeResult(rows=[
        (PARENT, 4, "ACME 000004 -0776", "RE: Q3 planning", "Some title", "email"),
    ]))])
    refs = asyncio.run(parent_refs(db, [
        (ATT, 4, "ACME 000004 -0776 .0001", "ACME 000004 -0776"),
        (PARENT, 4, "ACME 000004 -0776", "ACME 000004 -0776"),
        (LOOSE, 4, "ACME 000009", None),
    ]))
    assert set(refs) == {ATT}
    ref = refs[ATT]
    assert ref.id == PARENT and ref.bates_begin == "ACME 000004 -0776"
    assert ref.title == "RE: Q3 planning"  # subject beats title for emails
    assert ref.is_email is True
    sql = db.executed[0]
    assert "production_id" in sql and "bates_begin" in sql


def test_parent_refs_falls_back_to_title_then_number():
    db = FakeSession(responders=[("FROM documents", FakeResult(rows=[
        (PARENT, 4, "ZIP 000001", None, "archive.zip", "archive"),
    ]))])
    refs = asyncio.run(parent_refs(db, [(ATT, 4, "ZIP 000001 .0001", "ZIP 000001")]))
    assert refs[ATT].title == "archive.zip" and refs[ATT].is_email is False


def test_parent_refs_skips_query_without_attachments():
    db = FakeSession()
    assert asyncio.run(parent_refs(db, [(LOOSE, 4, "X 1", None)])) == {}
    assert db.executed == []


def test_parent_refs_query_compiles_for_postgres():
    from app.services.families import _parents_query
    sql = str(_parents_query([(4, "A 1"), (5, "B 2")]).compile(dialect=postgresql.dialect()))
    assert "(documents.production_id, documents.bates_begin) IN" in sql


def test_parent_refs_for_ids_looks_up_families_then_parents():
    from app.services.families import parent_refs_for_ids
    db = FakeSession(responders=[
        ("documents.family_id", FakeResult(rows=[
            (ATT, 4, "ACME 000004 -0776 .0001", "ACME 000004 -0776"),
            (LOOSE, 4, "ACME 000009", None),
        ])),
        ("email_subject", FakeResult(rows=[
            (PARENT, 4, "ACME 000004 -0776", "RE: Q3 planning", None, "email"),
        ])),
    ])
    refs = asyncio.run(parent_refs_for_ids(db, [ATT, LOOSE]))
    assert set(refs) == {ATT} and refs[ATT].title == "RE: Q3 planning"
    assert asyncio.run(parent_refs_for_ids(FakeSession(), [])) == {}


def test_search_results_carry_parent(monkeypatch):
    import app.routers.search as rs
    from tests.fakes import FakeUser

    async def fake_accessible(db, user):
        return [4]

    async def fake_search(db, q, **kw):
        return [{"id": ATT, "production_id": 4, "bates_begin": "ACME 000004 -0776 .0001",
                 "bates_end": "ACME 000004 -0776 .0001", "page_count": 1, "title": "Timeline.docx",
                 "snippet": "", "rank": 0.0},
                {"id": LOOSE, "production_id": 4, "bates_begin": "ACME 000009",
                 "bates_end": "ACME 000009", "page_count": 1, "title": "x", "snippet": "", "rank": 0.0}], 2

    async def fake_log(*a, **k):
        pass

    from app.services.families import ParentRef

    async def fake_refs(db, ids):
        return {ATT: ParentRef(PARENT, "ACME 000004 -0776", "RE: Q3 planning", True)}

    monkeypatch.setattr(rs, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rs, "search_documents", fake_search)
    monkeypatch.setattr(rs, "log_action", fake_log)
    monkeypatch.setattr(rs, "parent_refs_for_ids", fake_refs)
    out = asyncio.run(rs.search(
        q="timeline", production_id=4, page=1, per_page=50, sort="relevance", mode="fulltext",
        metadata=None, file_type=None, source_party=None, source_type=None,
        email_address=None, email_scope="from", date_from=None, date_to=None, tz=None,
        db=FakeSession(), user=FakeUser()))
    att, loose = out.results
    assert att.parent_id == PARENT and att.parent_bates == "ACME 000004 -0776"
    assert att.parent_title == "RE: Q3 planning" and att.parent_is_email is True
    assert loose.parent_id is None and loose.parent_title is None


def test_document_detail_carries_parent(monkeypatch):
    import app.routers.documents as rd
    from app.services.families import ParentRef

    class Doc:
        id = ATT; production_id = 4; bates_begin = "ACME 000004 -0776 .0001"
        bates_end = bates_begin; page_count = 1; title = "Timeline.docx"; summary = None
        processing_status = "complete"; metadata_ = {}; text_content = ""; native_path = None
        image_paths = []; file_type = "docx"; tags = []

    async def fake_refs(db, ids):
        return {ATT: ParentRef(PARENT, "ACME 000004 -0776", "RE: Q3 planning", True)}

    monkeypatch.setattr(rd, "parent_refs_for_ids", fake_refs)
    db = FakeSession(responders=[("count", FakeResult(scalar=0))])
    out = asyncio.run(rd._doc_detail(Doc(), db))
    assert out.parent_id == PARENT and out.parent_title == "RE: Q3 planning"
