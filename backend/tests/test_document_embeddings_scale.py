"""get_document_embeddings must scale to huge matters.

asyncpg caps a statement at 32,767 bind parameters. The old implementation
loaded every document id into Python and sent them back as
``document_chunks.document_id IN (...)``, which blew up clustering on a
~69k-document matter. The query must select by production instead, in one
statement with a constant number of bind parameters.
"""

import asyncio
import uuid

from sqlalchemy.dialects import postgresql

from app.services import document_embeddings as de
from tests.fakes import FakeResult


class _RecordingSession:
    """Answers any id-list query with 40,000 ids and records every statement."""

    def __init__(self, n_docs=40_000):
        self.ids = [uuid.uuid4() for _ in range(n_docs)]
        self.statements = []

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        sql = str(stmt)
        if "avg" in sql.lower():
            return FakeResult(rows=[(self.ids[0], "[0.5,0.25]"), (self.ids[1], None)])
        return FakeResult(rows=[(i,) for i in self.ids])


def _bind_count(stmt) -> int:
    """Bind parameters the driver will see: an expanding IN (...) becomes one
    parameter per element; an array literal (ids_match) stays one."""
    compiled = stmt.compile(dialect=postgresql.dialect())
    n = 0
    for key, value in compiled.params.items():
        bind = compiled.binds.get(key)
        if bind is not None and bind.expanding:
            n += len(value)
        else:
            n += 1
    return n


def test_get_document_embeddings_issues_one_statement_without_per_document_binds():
    db = _RecordingSession()
    out = asyncio.run(de.get_document_embeddings(db, production_id=7))

    assert len(db.statements) == 1, "must not pre-load document ids"
    stmt = db.statements[0]
    assert _bind_count(stmt) < 10
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert 7 in compiled.params.values()
    sql = str(compiled)
    assert "documents.production_id" in sql
    assert "documents.text_content IS NOT NULL" in sql
    # Parsing of the averaged vector is unchanged; NULL averages are skipped.
    assert list(out) == [str(db.ids[0])]
    assert out[str(db.ids[0])].tolist() == [0.5, 0.25]


def test_select_diverse_sample_averages_embeddings_by_production_not_id_list():
    from app.services import sampling

    db = _RecordingSession()
    asyncio.run(sampling.select_diverse_sample(db, production_id=7, sample_size=10))
    avg_stmts = [s for s in db.statements if "avg" in str(s).lower()]
    assert len(avg_stmts) == 1
    assert _bind_count(avg_stmts[0]) < 10
    assert "documents.production_id" in str(avg_stmts[0])


def test_detect_duplicates_clears_old_groups_without_binding_each_id(monkeypatch):
    import app.services.duplicates as dup

    class _Session(_RecordingSession):
        async def execute(self, stmt, params=None):
            self.statements.append(stmt)
            # 40k old groups would come back from any id lookup.
            return FakeResult(rows=[(i, "x") for i in range(40_000)])

        async def commit(self):
            pass

        async def flush(self):
            pass

        def add(self, obj):
            pass

    async def no_embeddings(db, production_id):
        return {}

    db = _Session()
    monkeypatch.setattr(dup, "get_document_embeddings", no_embeddings)
    monkeypatch.setattr(dup, "_compute_groups", lambda *a: ([], [], []))
    asyncio.run(dup.detect_duplicates(db, 7))
    deletes = [s for s in db.statements if str(s).startswith("DELETE")]
    assert deletes, "old groups must be cleared"
    assert all(_bind_count(s) < 10 for s in deletes)


# --- id lists that can't be replaced by a production query: batch at 5,000 ---

class _BatchSession:
    def __init__(self):
        self.statements = []

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        return FakeResult(rows=[])


def _assert_batched(statements, n_ids):
    expected = -(-n_ids // 5000)
    assert len(statements) == expected
    assert all(_bind_count(s) <= 5000 + 5 for s in statements)


def test_merge_undo_loads_moved_rows_in_batches():
    from app.models import EntityMention
    from app.services import entity_merge

    db = _BatchSession()
    ids = list(range(12_001))
    asyncio.run(entity_merge._rows_by_ids(db, EntityMention, ids))
    _assert_batched(db.statements, len(ids))


def test_sample_tag_counts_query_in_batches():
    from types import SimpleNamespace
    from app.routers import sampling as rs

    db = _BatchSession()
    smp = SimpleNamespace(document_ids=[str(uuid.uuid4()) for _ in range(12_001)])
    asyncio.run(rs._tagged_in_sample(db, smp, tag_id=3))
    _assert_batched(db.statements, 12_001)


def test_tar_validation_tag_members_query_in_batches():
    from app.services import tar_validation as tv

    db = _BatchSession()
    ids = [str(uuid.uuid4()) for _ in range(12_001)]
    asyncio.run(tv._tag_members(db, 3, ids))
    _assert_batched(db.statements, 12_001)
