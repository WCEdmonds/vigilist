"""What a `select(Document)` is allowed to pull over the wire.

Prod Postgres is cross-cloud from Cloud Run, so every column loaded is billed
egress. `documents.text_search_vector` is ~8.5MB across the corpus and is never
read as a Python value — it exists for the GIN index and for SQL expressions —
so loading it on every entity query was pure waste. It is deferred at the
mapper level.

These tests pin that, and pin the three things deferral could have broken.
"""

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from app.models import Document


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_document_select_does_not_load_the_search_vector():
    # The regression this guards: undeferring it (or redeclaring the column
    # without deferred()) silently restores ~8.5MB of egress across 14 query
    # sites, with no test failure anywhere else to notice it.
    assert "text_search_vector" not in _sql(select(Document))


def test_search_vector_still_usable_in_a_match_expression():
    # Deferral is a mapper loading concern; the column object must still work
    # in WHERE. If this breaks, full-text search silently matches nothing.
    tsquery = func.plainto_tsquery("english", "taser")
    sql = _sql(select(Document.id).where(Document.text_search_vector.op("@@")(tsquery)))
    assert "text_search_vector" in sql
    assert "@@" in sql


def test_search_vector_still_usable_for_ranking():
    tsquery = func.plainto_tsquery("english", "taser")
    sql = _sql(select(func.ts_rank(Document.text_search_vector, tsquery)))
    assert "ts_rank" in sql
    assert "text_search_vector" in sql


def test_gin_index_still_declared_on_the_table():
    # Deferral must not disturb the table definition — without this index,
    # full-text search degrades to a sequential scan.
    by_name = {i.name: [c.name for c in i.columns] for i in Document.__table__.indexes}
    assert by_name.get("ix_documents_text_search") == ["text_search_vector"]


def test_text_content_is_still_loaded_by_default():
    # Deliberately NOT deferred: 45 call sites read this attribute, and a lazy
    # load in async context raises MissingGreenlet (the failure mode behind
    # #86). It gets excluded per-query with load_only() instead, so a global
    # deferral appearing here would be a bug, not an improvement.
    assert "text_content" in _sql(select(Document))
