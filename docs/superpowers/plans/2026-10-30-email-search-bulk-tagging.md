# Email-Address Search + Bulk Tag-by-Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a reviewer search emails by address (sender only or any participant) and by date range, sort by date, and apply any tag — privileged, Not Responsive, etc. — to every matching document plus attachments, with a dry-run count, a switch/leave prompt for conflicting responsiveness tags, and optional bulk privilege-log overrides.

**Architecture:** Search filter-building is factored into `build_search_conditions` so `/api/search` and the new `POST /api/documents/bulk-tag-by-query` resolve the identical set. A new `services/bulk_tagging.py` holds a pure planner (`plan_tagging`) plus small DB helpers that pass ID sets as one Postgres array bind. The frontend adds an email filter to the Omnibox, a Gmail-style "Select all N matching", and a `BulkTagDialog` that always dry-runs before applying.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async (asyncpg, Postgres), Pydantic v2, pytest (fake-session tests, no DB); React + TypeScript (Vite, no test framework, eslint with React Compiler rules).

**Spec:** `docs/superpowers/specs/2026-10-30-email-search-bulk-tagging-design.md`

**Worktree:** `F:\Users\WCEdm\Documents\Developer\descubre-bulkpriv`, branch `feat/email-search-bulk-privilege` (from `origin/main` @ 1af959c). All paths below are relative to it. Backend commands run from `backend/`; frontend from `frontend/`.

## Global Constraints

- User-facing copy says **"attachments"**, never "family". Internally expansion uses `Document.family_id`.
- Email match: case-insensitive substring; LIKE metacharacters `%`, `_`, `\` in input are literal.
- Document date = `COALESCE(date_sent, date_created)`; `date_from`/`date_to` are inclusive whole days (UTC); undated docs never match a date bound and sort last. Sort values: search `relevance|bates|date_asc|date_desc`, browse adds `date_asc|date_desc`.
- `email_scope` values are exactly `"from"` (default) and `"any"`. UI labels: **Sender only** / **Any participant**.
- Exclusive categories: `EXCLUSIVE_CATEGORIES = {"responsiveness"}` only.
- `conflict_action` values are exactly `"switch"` and `"leave"`; apply with unresolved conflicts ⇒ HTTP 409, detail `"conflicts_unresolved"`.
- Hard cap `MAX_BULK_DOCS = 50_000` per call ⇒ 422.
- Override fields (`disposition`, `description`) only with a privilege tag (else 422) and only for manager+ (else 403). `disposition` ∈ `withhold | redact_in_part | produce`.
- Role checks run for **every production** touched, not just the first.
- Never pass large ID lists as `IN (...)` — asyncpg caps a statement at 32,767 bind parameters. Use `ids_match()` (one array bind).
- Nothing in `services/bulk_tagging.py` commits; the router commits once.
- Frontend: no `setState` in render or synchronously in effects, no `.current` reads/writes in render (React Compiler lint). Touched-file lint must not exceed baseline (`src/App.tsx` baseline is 7 errors / 1 warning); new files lint clean.
- Backend baseline: `python -m pytest -q` ⇒ 878 passed, 2 skipped.

## Review Focus

1. **Underscore in an email address** (`j_smith@firm.com`) — `_` is a LIKE wildcard; must match literally, not as "any char". Pinned in Task 1 (`test_metacharacters_escaped_in_bind_value`).
2. **Very large result sets (10k–50k docs)** — every ID-set query must use a single array bind, or asyncpg errors past 32,767 params. Pinned in Task 2 (`test_ids_match_is_single_array_bind`, `test_apply_plan_*` assert `= ANY`).
3. **Control numbers repeat across productions** — attachment expansion must join on `production_id` too, or tagging one production's email pulls in another's documents. Pinned in Task 2 (`test_expand_attachments_joins_on_production`).
4. **"To" date the reviewer types (e.g. 3/31)** — must include emails sent any time on 3/31, not stop at midnight. Pinned in Task 1 (`test_date_range_is_inclusive_whole_days`).
5. **Semantic-mode search** — results are a vector ranking a query can't reproduce, so "Select all N matching" must not be offered; otherwise the bulk action would tag a different (full-text) set. Pinned in Task 6 manual verification step and in Task 5's `searchUsedFulltext` gate.

---

### Task 1: Email participant + date filters and date sort in search

**Files:**
- Modify: `backend/app/services/search.py` (imports; new helpers; `search_documents` at :103-205)
- Modify: `backend/app/routers/search.py:18-71`
- Modify: `backend/app/routers/documents.py:73` (sort pattern) and `:173-186` (order_by branches)
- Modify: `backend/app/schemas.py:204` (`SearchResult`)
- Test: `backend/tests/test_search_email_filter.py` (create)

**Interfaces:**
- Produces:
  - `escape_like(value: str) -> str`
  - `email_address_condition(address: str, scope: str = "from") -> ColumnElement[bool]`
  - `document_date() -> ColumnElement` — `COALESCE(date_sent, date_created)`
  - `date_order(direction: str) -> list` — `[document_date() ASC|DESC NULLS LAST, bates_begin]`
  - `build_search_conditions(query: str = "", *, production_id=None, accessible_production_ids=None, metadata_filters=None, file_type=None, source_party=None, source_type=None, email_address: str | None = None, email_scope: str = "from", date_from: date | None = None, date_to: date | None = None) -> tuple[list, object | None, bool]` — returns `(conditions, tsquery_or_None, has_criteria)`
  - `search_documents(...)` gains kwargs `email_address`, `email_scope`, `date_from`, `date_to`; `sort` accepts `relevance | bates | date_asc | date_desc`; each result dict gains `"doc_date"`.
  - `SearchResult.doc_date: datetime | None = None`
  - `GET /api/search` gains `email_address`, `email_scope` (`^(from|any)$`), `date_from`, `date_to` (`YYYY-MM-DD`); `sort` pattern `^(relevance|bates|date_asc|date_desc)$`; reversed range ⇒ 422.
  - `GET /api/documents` `sort` pattern `^(bates|recent|size|date_asc|date_desc)$`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_search_email_filter.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search_email_filter.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_search_conditions'`.

- [ ] **Step 3: Implement helpers in `backend/app/services/search.py`**

Change the imports at the top to (keep the existing `AsyncSession` and `Document` imports):

```python
import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, literal_column, or_, select
```

Add above `async def search_documents` (after `build_tsquery` / `FILE_TYPE_EXTENSIONS`):

```python
def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input matches literally —
    email addresses routinely contain '_', which LIKE treats as a wildcard."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def email_address_condition(address: str, scope: str = "from"):
    """Case-insensitive substring match on the sender, or on any participant.

    The email_* columns hold raw header strings ('"Jane Doe" <jane@x.com>,
    ...'), so a substring match covers display-name forms and bare
    '@domain.com' alike.
    """
    pattern = f"%{escape_like(address.strip())}%"
    if scope == "any":
        cols = (Document.email_from, Document.email_to, Document.email_cc, Document.email_bcc)
    else:
        cols = (Document.email_from,)
    return or_(*(col.ilike(pattern, escape="\\") for col in cols))


def document_date():
    """A document's date: the sent date for emails, else the created date."""
    return func.coalesce(Document.date_sent, Document.date_created)


def date_order(direction: str) -> list:
    """ORDER BY document date with undated documents last, Bates as tiebreak."""
    d = document_date()
    return [(d.asc() if direction == "asc" else d.desc()).nulls_last(), Document.bates_begin]


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def build_search_conditions(
    query: str = "",
    *,
    production_id: int | None = None,
    accessible_production_ids: list[int] | None = None,
    metadata_filters: dict[str, str] | None = None,
    file_type: str | None = None,
    source_party: str | None = None,
    source_type: str | None = None,
    email_address: str | None = None,
    email_scope: str = "from",
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[list, object | None, bool]:
    """WHERE clauses for a search. Shared by /api/search and bulk
    tag-by-query so both resolve exactly the same document set.

    Returns (conditions, tsquery, has_criteria): tsquery is None without a
    text query; has_criteria False means the search would match nothing.
    Date bounds are inclusive whole days (UTC); undated documents never
    match a date bound.
    """
    tsquery_str = build_tsquery(query) if query else ""
    email = (email_address or "").strip()
    has_criteria = bool(tsquery_str or metadata_filters or file_type
                        or source_party or source_type or email
                        or date_from is not None or date_to is not None)

    conditions = []
    tsquery = None
    if accessible_production_ids is not None:
        conditions.append(Document.production_id.in_(accessible_production_ids))
    if production_id is not None:
        conditions.append(Document.production_id == production_id)
    if tsquery_str:
        # Bound parameter — build_tsquery sanitizes lexemes, but the value must
        # still never be interpolated into the SQL text itself.
        tsquery = func.to_tsquery("english", tsquery_str)
        conditions.append(Document.text_search_vector.op("@@")(tsquery))
    if metadata_filters:
        for key, value in metadata_filters.items():
            if not re.match(r'^[a-zA-Z0-9_ ]+$', key):
                from fastapi import HTTPException
                raise HTTPException(status_code=400, detail=f"Invalid metadata key: {key}")
            conditions.append(Document.metadata_[key].astext.ilike(f"%{value}%"))
    if file_type:
        if file_type == "native":
            conditions.append(Document.native_path.isnot(None))
        elif file_type == "images_only":
            conditions.append(Document.native_path.is_(None))
        elif file_type in FILE_TYPE_EXTENSIONS:
            exts = FILE_TYPE_EXTENSIONS[file_type]
            conditions.append(or_(*[func.lower(Document.native_path).like(f"%{ext}") for ext in exts]))
    if source_party:
        conditions.append(Document.source_party == source_party)
    if source_type == "received":
        conditions.append(Document.source_type == "received")
    elif source_type == "collection":
        # Undesignated (NULL) documents count as ours: only loads explicitly
        # marked as received are "incoming". Keeps the toggle meaningful on
        # matters ingested before source designation existed.
        conditions.append(Document.source_type.is_distinct_from("received"))
    if email:
        conditions.append(email_address_condition(email, email_scope))
    if date_from is not None:
        conditions.append(document_date() >= _day_start(date_from))
    if date_to is not None:
        conditions.append(document_date() < _day_start(date_to + timedelta(days=1)))

    return conditions, tsquery, has_criteria
```

- [ ] **Step 4: Rework `search_documents`** — replace its signature and body up to (not including) `if has_text_query:\n        rank = func.ts_rank(...)` with:

```python
async def search_documents(
    db: AsyncSession,
    query: str,
    production_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
    sort: str = "relevance",
    accessible_production_ids: list[int] | None = None,
    metadata_filters: dict[str, str] | None = None,
    file_type: str | None = None,
    source_party: str | None = None,
    source_type: str | None = None,
    email_address: str | None = None,
    email_scope: str = "from",
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[list[dict], int]:
    """Execute a full-text search and return results with snippets."""
    conditions, tsquery, has_criteria = build_search_conditions(
        query,
        production_id=production_id,
        accessible_production_ids=accessible_production_ids,
        metadata_filters=metadata_filters,
        file_type=file_type,
        source_party=source_party,
        source_type=source_type,
        email_address=email_address,
        email_scope=email_scope,
        date_from=date_from,
        date_to=date_to,
    )
    if not has_criteria:
        return [], 0
    has_text_query = tsquery is not None
```

Further down in the same function, change the results query and ordering to:

```python
    q = (
        select(Document, rank, headline, document_date().label("doc_date"))
        .where(*conditions)
    )

    if sort == "bates":
        q = q.order_by(Document.bates_begin)
    elif sort == "date_asc":
        q = q.order_by(*date_order("asc"))
    elif sort == "date_desc":
        q = q.order_by(*date_order("desc"))
    else:
        q = q.order_by(rank.desc())
```

and the result loop to:

```python
    for doc, rank_val, snippet, doc_date in rows:
        results.append({
            "id": doc.id,
            "production_id": doc.production_id,
            "bates_begin": doc.bates_begin,
            "bates_end": doc.bates_end,
            "page_count": doc.page_count,
            "title": doc.title,
            "snippet": snippet,
            "rank": float(rank_val),
            "doc_date": doc_date,
        })
```

- [ ] **Step 5: Schema** — in `backend/app/schemas.py` `class SearchResult`, add after `rank: float`:

```python
    doc_date: datetime | None = None
```

- [ ] **Step 6: Search router** — in `backend/app/routers/search.py` add `from datetime import date`; change the `sort` param to:

```python
    sort: str = Query("relevance", pattern="^(relevance|bates|date_asc|date_desc)$"),
```

add after `source_type`:

```python
    email_address: str | None = Query(None, description="Email address or @domain to match"),
    email_scope: str = Query("from", pattern="^(from|any)$"),
    date_from: date | None = Query(None, description="Inclusive start date (YYYY-MM-DD)"),
    date_to: date | None = Query(None, description="Inclusive end date (YYYY-MM-DD)"),
```

after the metadata JSON parsing block add:

```python
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from is after date_to")
```

change the semantic gate to:

```python
    has_email = bool(email_address and email_address.strip())
    has_dates = date_from is not None or date_to is not None
    if (mode == "semantic" and q.strip() and not metadata_filters and not file_type
            and not source_party and not source_type and not has_email and not has_dates):
```

and add to the `search_documents(...)` call:

```python
            email_address=email_address,
            email_scope=email_scope,
            date_from=date_from,
            date_to=date_to,
```

- [ ] **Step 7: Browse-list date sort** — in `backend/app/routers/documents.py` add `from app.services.search import date_order`, change the `sort` param pattern to `"^(bates|recent|size|date_asc|date_desc)$"`, and add before the final `else:` of the sort chain (~:185):

```python
    elif sort in ("date_asc", "date_desc"):
        query = query.order_by(*date_order("asc" if sort == "date_asc" else "desc"))
```

- [ ] **Step 8: Run tests**

Run: `python -m pytest tests/test_search_email_filter.py -q` → all PASS.
Run: `python -m pytest -q` → 893 passed, 2 skipped (baseline 878 + 15).

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/search.py backend/app/routers/search.py backend/app/routers/documents.py backend/app/schemas.py backend/tests/test_search_email_filter.py
git commit -m "feat(search): email participant and date-range filters, date sort"
```

---

### Task 2: Bulk tagging service

**Files:**
- Create: `backend/app/services/bulk_tagging.py`
- Modify: `backend/app/schemas.py` (after `BulkTagRequest`, ~:86; import line :5)
- Modify: `backend/tests/fakes.py` (`FakeSession.execute` accepts params)
- Test: `backend/tests/test_bulk_tagging_service.py` (create)

**Interfaces:**
- Consumes: `build_search_conditions` (Task 1).
- Produces (schemas):
  - `BulkTagQuery(q: str = "", production_id: int | None, metadata: dict[str, str] | None, file_type: str | None, source_party: str | None, source_type: Literal["collection","received"] | None, email_address: str | None, email_scope: Literal["from","any"] = "from", date_from: date | None, date_to: date | None)`
  - `BulkTagByQueryRequest(doc_ids: list[UUID] | None, query: BulkTagQuery | None, tag_id: int, include_attachments: bool = True, conflict_action: Literal["switch","leave"] | None, disposition: str | None, description: str | None, dry_run: bool = False)` — exactly one of `doc_ids`/`query`, `doc_ids` non-empty.
- Produces (`app.services.bulk_tagging`):
  - `EXCLUSIVE_CATEGORIES: frozenset[str]`, `MAX_BULK_DOCS: int`
  - `class NoCriteriaError(ValueError)`
  - `ids_match(column, ids) -> ColumnElement[bool]`
  - `async resolve_query_docs(db, query: BulkTagQuery, accessible_production_ids: list[int]) -> dict[UUID, int]` (doc id → production id; at most `MAX_BULK_DOCS + 1` rows)
  - `async resolve_explicit_docs(db, doc_ids: list[UUID]) -> dict[UUID, int]`
  - `async expand_attachments(db, docs: dict[UUID, int]) -> dict[UUID, int]`
  - `async already_tagged_ids(db, tag_id: int, doc_ids) -> set[UUID]`
  - `async conflicting_tags(db, tag, doc_ids) -> tuple[dict[UUID, set[int]], dict[int, str]]`
  - `@dataclass(frozen=True) TagPlan(to_tag: frozenset, already_tagged: frozenset, conflicts: dict, switch: dict, left: frozenset)` with property `coded -> frozenset`
  - `plan_tagging(target_ids, already_tagged, conflicts, conflict_action) -> TagPlan`
  - `conflict_tag_counts(conflicts: dict, names: dict[int, str]) -> list[dict]` (`{"tag_id","name","count"}`, count desc then name)
  - `async apply_plan(db, plan: TagPlan, tag_id: int, user_id: str, override: dict) -> int` (returns overridden count)

- [ ] **Step 1: Let `FakeSession.execute` accept bulk params** — in `backend/tests/fakes.py`, replace `FakeSession.execute` and add `executed_params` in `__init__`:

```python
        self.executed_params = []
```

```python
    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append(sql)
        self.executed_params.append(params)
        for substring, result in self.responders:
            if substring in sql:
                return result(sql) if callable(result) else result
        return FakeResult()
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_bulk_tagging_service.py`:

```python
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
    assert "email_from ILIKE" in db.executed[0]
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_bulk_tagging_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.bulk_tagging'`.

- [ ] **Step 4: Add schemas** — in `backend/app/schemas.py` change `from pydantic import UUID4, BaseModel` to `from pydantic import UUID4, BaseModel, model_validator`, then add after `BulkTagRequest`:

```python
class BulkTagQuery(BaseModel):
    """The /api/search filter fields, so a bulk action resolves the same set
    the reviewer is looking at."""
    q: str = ""
    production_id: int | None = None
    metadata: dict[str, str] | None = None
    file_type: str | None = None
    source_party: str | None = None
    source_type: Literal["collection", "received"] | None = None
    email_address: str | None = None
    email_scope: Literal["from", "any"] = "from"
    date_from: date | None = None
    date_to: date | None = None


class BulkTagByQueryRequest(BaseModel):
    doc_ids: list[UUID] | None = None
    query: BulkTagQuery | None = None
    tag_id: int
    include_attachments: bool = True
    conflict_action: Literal["switch", "leave"] | None = None
    disposition: str | None = None
    description: str | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if (self.doc_ids is None) == (self.query is None):
            raise ValueError("Provide exactly one of doc_ids or query")
        if self.doc_ids is not None and not self.doc_ids:
            raise ValueError("doc_ids must not be empty")
        return self
```

- [ ] **Step 5: Create `backend/app/services/bulk_tagging.py`**

```python
"""Bulk tag-by-query: resolve a search (or explicit IDs) to documents, pull in
attachments, detect conflicting responsiveness tags, and apply one tag.

plan_tagging is pure so the decision logic is testable without a database;
the async helpers are thin queries. Every ID set is passed as ONE Postgres
array bind (ids_match) — asyncpg caps a statement at 32,767 parameters and a
production-wide action can touch tens of thousands of documents. Nothing
here commits; the router owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import and_, any_, delete, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.models import Document, DocumentTag, Tag
from app.schemas import BulkTagQuery
from app.services.search import build_search_conditions

# Categories where a document should carry at most one tag — applying one
# tag in the category conflicts with any other already on the document.
EXCLUSIVE_CATEGORIES = frozenset({"responsiveness"})
MAX_BULK_DOCS = 50_000


class NoCriteriaError(ValueError):
    """The query has no text or filters, so it would match nothing."""


def ids_match(column, ids):
    """`column = ANY(:ids)` with a single array bind parameter."""
    return column == any_(literal(list(ids), ARRAY(PG_UUID(as_uuid=True))))


async def resolve_query_docs(
    db, query: BulkTagQuery, accessible_production_ids: list[int]
) -> dict[UUID, int]:
    """Every document the equivalent /api/search would return (unpaged),
    capped at MAX_BULK_DOCS + 1 so the caller can detect overflow."""
    conditions, _tsquery, has_criteria = build_search_conditions(
        query.q,
        production_id=query.production_id,
        accessible_production_ids=accessible_production_ids,
        metadata_filters=query.metadata or None,
        file_type=query.file_type,
        source_party=query.source_party,
        source_type=query.source_type,
        email_address=query.email_address,
        email_scope=query.email_scope,
        date_from=query.date_from,
        date_to=query.date_to,
    )
    if not has_criteria:
        raise NoCriteriaError
    rows = (await db.execute(
        select(Document.id, Document.production_id)
        .where(*conditions)
        .limit(MAX_BULK_DOCS + 1)
    )).all()
    return {doc_id: prod_id for doc_id, prod_id in rows}


async def resolve_explicit_docs(db, doc_ids: list[UUID]) -> dict[UUID, int]:
    rows = (await db.execute(
        select(Document.id, Document.production_id).where(ids_match(Document.id, doc_ids))
    )).all()
    return {doc_id: prod_id for doc_id, prod_id in rows}


async def expand_attachments(db, docs: dict[UUID, int]) -> dict[UUID, int]:
    """Add every document sharing a family_id with a target. Scoped to the
    same production: control numbers repeat across productions."""
    if not docs:
        return {}
    seed = aliased(Document)
    rows = (await db.execute(
        select(Document.id, Document.production_id)
        .join(seed, and_(
            seed.family_id == Document.family_id,
            seed.production_id == Document.production_id,
        ))
        .where(ids_match(seed.id, docs.keys()), seed.family_id.is_not(None))
        .distinct()
    )).all()
    expanded = dict(docs)
    for doc_id, prod_id in rows:
        expanded.setdefault(doc_id, prod_id)
    return expanded


async def already_tagged_ids(db, tag_id: int, doc_ids) -> set[UUID]:
    if not doc_ids:
        return set()
    rows = (await db.execute(
        select(DocumentTag.document_id).where(
            DocumentTag.tag_id == tag_id, ids_match(DocumentTag.document_id, doc_ids))
    )).all()
    return {row[0] for row in rows}


async def conflicting_tags(db, tag, doc_ids) -> tuple[dict[UUID, set[int]], dict[int, str]]:
    """Other tags in the chosen tag's exclusive category, per document."""
    if tag.category not in EXCLUSIVE_CATEGORIES or not doc_ids:
        return {}, {}
    rows = (await db.execute(
        select(DocumentTag.document_id, Tag.id, Tag.name)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(
            Tag.category == tag.category,
            Tag.id != tag.id,
            ids_match(DocumentTag.document_id, doc_ids),
        )
    )).all()
    by_doc: dict[UUID, set[int]] = {}
    names: dict[int, str] = {}
    for doc_id, tag_id, name in rows:
        by_doc.setdefault(doc_id, set()).add(tag_id)
        names[tag_id] = name
    return by_doc, names


@dataclass(frozen=True)
class TagPlan:
    to_tag: frozenset            # docs that get a new DocumentTag
    already_tagged: frozenset    # targets that already carry the tag
    conflicts: dict = field(default_factory=dict)  # doc -> other exclusive tag ids
    switch: dict = field(default_factory=dict)     # conflicts to delete ("switch")
    left: frozenset = frozenset()                  # conflicting docs skipped ("leave")

    @property
    def coded(self) -> frozenset:
        """Docs that end up carrying the tag — the override targets."""
        return self.to_tag | self.already_tagged


def plan_tagging(target_ids, already_tagged, conflicts, conflict_action) -> TagPlan:
    """Decide what happens to each target. A doc that already has the chosen
    tag is never a conflict. conflict_action None reports conflicts without
    resolving them (dry run); the router refuses to apply in that state."""
    targets = set(target_ids)
    already = set(already_tagged) & targets
    pending = {d: set(t) for d, t in conflicts.items() if d in targets and d not in already}
    left = set(pending) if conflict_action == "leave" else set()
    switch = dict(pending) if conflict_action == "switch" else {}
    return TagPlan(
        to_tag=frozenset(targets - already - left),
        already_tagged=frozenset(already),
        conflicts=pending,
        switch=switch,
        left=frozenset(left),
    )


def conflict_tag_counts(conflicts: dict, names: dict[int, str]) -> list[dict]:
    counts: dict[int, int] = {}
    for tag_ids in conflicts.values():
        for tag_id in tag_ids:
            counts[tag_id] = counts.get(tag_id, 0) + 1
    return sorted(
        ({"tag_id": t, "name": names.get(t, ""), "count": n} for t, n in counts.items()),
        key=lambda c: (-c["count"], c["name"]),
    )


async def apply_plan(db, plan: TagPlan, tag_id: int, user_id: str, override: dict) -> int:
    """Write the plan. Returns how many documents got the override."""
    if plan.switch:
        stale_tag_ids = set().union(*plan.switch.values())
        await db.execute(
            delete(DocumentTag)
            .where(ids_match(DocumentTag.document_id, plan.switch.keys()),
                   DocumentTag.tag_id.in_(stale_tag_ids))
            .execution_options(synchronize_session=False)
        )
    if plan.to_tag:
        await db.execute(
            pg_insert(DocumentTag).on_conflict_do_nothing(constraint="uq_doc_tag"),
            [{"document_id": d, "tag_id": tag_id, "applied_by": user_id} for d in plan.to_tag],
        )
    if override and plan.coded:
        await db.execute(
            update(Document)
            .where(ids_match(Document.id, plan.coded))
            .values(**override)
            .execution_options(synchronize_session=False)
        )
        return len(plan.coded)
    return 0
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_bulk_tagging_service.py -q` → all PASS.
If `test_expand_attachments_joins_on_production` fails only on the alias spelling, print `db.executed[0]` and adjust the asserted substrings to the actual alias name (`documents_1`) — the requirement is that both `family_id` and `production_id` equalities appear in the JOIN.
Run: `python -m pytest -q` → all pass (baseline + Task 1 + these).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/bulk_tagging.py backend/app/schemas.py backend/tests/fakes.py backend/tests/test_bulk_tagging_service.py
git commit -m "feat(tags): bulk tagging service with attachment expansion and conflict planning"
```

---

### Task 3: `POST /api/documents/bulk-tag-by-query` endpoint

**Files:**
- Modify: `backend/app/routers/tags.py` (imports at :1-20; new endpoint after `bulk_tag` ~:222)
- Test: `backend/tests/test_bulk_tag_by_query_endpoint.py` (create)

**Interfaces:**
- Consumes: everything Task 2 produces; `DISPOSITIONS` from `app.services.privilege`.
- Produces: `bulk_tag_by_query(body: BulkTagByQueryRequest, db, user) -> dict`.
  - Dry run → `{matched, attachments_added, total, already_tagged, conflicts, conflict_tags}`
  - Apply → `{total, tagged, already_tagged, switched, left, overridden}`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_bulk_tag_by_query_endpoint.py`:

```python
"""Fake-session tests for POST /api/documents/bulk-tag-by-query."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.tags as rt
from app.schemas import BulkTagByQueryRequest, BulkTagQuery
from tests.fakes import FakeSession, FakeUser

A, B, C = (uuid4() for _ in range(3))


class FakeTag:
    def __init__(self, tag_id=5, category="responsiveness", name="Not Responsive",
                 is_privilege=False, production_id=None):
        self.id = tag_id
        self.category = category
        self.name = name
        self.is_privilege = is_privilege
        self.production_id = production_id


def _patch(monkeypatch, *, roles=None, accessible=(1, 2), matched=None, family=None,
           already=(), conflicts=None, conflict_names=None):
    """roles: production_id -> role (default reviewer everywhere)."""
    roles = roles or {}
    state = {"apply": None, "audit": [], "expanded": False}
    matched = matched if matched is not None else {A: 1, B: 1}

    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return roles.get(production_id, "reviewer")

    async def fake_log(db, user, action, resource_type, resource_id=None, **kwargs):
        state["audit"].append((action, kwargs))

    async def fake_resolve_query(db, query, acc):
        return dict(matched)

    async def fake_resolve_explicit(db, doc_ids):
        return dict(matched)

    async def fake_expand(db, docs):
        state["expanded"] = True
        return {**docs, **(family or {})}

    async def fake_already(db, tag_id, ids):
        return set(already)

    async def fake_conflicts(db, tag, ids):
        return dict(conflicts or {}), dict(conflict_names or {})

    async def fake_apply(db, plan, tag_id, user_id, override):
        state["apply"] = (plan, tag_id, user_id, override)
        return len(plan.coded) if override else 0

    monkeypatch.setattr(rt, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rt, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rt, "log_action", fake_log)
    monkeypatch.setattr(rt.bt, "resolve_query_docs", fake_resolve_query)
    monkeypatch.setattr(rt.bt, "resolve_explicit_docs", fake_resolve_explicit)
    monkeypatch.setattr(rt.bt, "expand_attachments", fake_expand)
    monkeypatch.setattr(rt.bt, "already_tagged_ids", fake_already)
    monkeypatch.setattr(rt.bt, "conflicting_tags", fake_conflicts)
    monkeypatch.setattr(rt.bt, "apply_plan", fake_apply)
    return state


def _run(body, tag=None):
    db = FakeSession(get_objects={("Tag", 5): tag} if tag else {})
    out = asyncio.run(rt.bulk_tag_by_query(body=body, db=db, user=FakeUser()))
    return out, db


def _query_body(**kw):
    return BulkTagByQueryRequest(tag_id=5, query=BulkTagQuery(email_address="jane@x.com"), **kw)


def _raises(body, tag, status):
    with pytest.raises(HTTPException) as exc:
        _run(body, tag)
    assert exc.value.status_code == status
    return exc.value


def test_unknown_tag_404(monkeypatch):
    _patch(monkeypatch)
    _raises(_query_body(), None, 404)


def test_override_on_non_privilege_tag_422(monkeypatch):
    _patch(monkeypatch, roles={1: "manager"})
    _raises(_query_body(disposition="withhold"), FakeTag(), 422)


def test_invalid_disposition_422(monkeypatch):
    _patch(monkeypatch, roles={1: "manager"})
    _raises(_query_body(disposition="burn"), FakeTag(is_privilege=True, category="privilege"), 422)


def test_query_without_criteria_422(monkeypatch):
    _patch(monkeypatch)

    async def raise_no_criteria(db, query, acc):
        raise rt.bt.NoCriteriaError

    monkeypatch.setattr(rt.bt, "resolve_query_docs", raise_no_criteria)
    _raises(_query_body(), FakeTag(), 422)


def test_dry_run_returns_counts_and_writes_nothing(monkeypatch):
    state = _patch(monkeypatch, family={C: 1}, already=(A,),
                   conflicts={B: {1}}, conflict_names={1: "Responsive"})
    out, db = _run(_query_body(dry_run=True), FakeTag())
    assert out == {
        "matched": 2, "attachments_added": 1, "total": 3, "already_tagged": 1,
        "conflicts": 1, "conflict_tags": [{"tag_id": 1, "name": "Responsive", "count": 1}],
    }
    assert state["apply"] is None
    assert db.commits == 0


def test_apply_with_unresolved_conflicts_409(monkeypatch):
    state = _patch(monkeypatch, conflicts={B: {1}}, conflict_names={1: "Responsive"})
    err = _raises(_query_body(), FakeTag(), 409)
    assert err.detail == "conflicts_unresolved"
    assert state["apply"] is None


def test_apply_switch_commits_and_audits(monkeypatch):
    state = _patch(monkeypatch, conflicts={B: {1}}, conflict_names={1: "Responsive"})
    out, db = _run(_query_body(conflict_action="switch"), FakeTag())
    assert out == {"total": 2, "tagged": 2, "already_tagged": 0, "switched": 1, "left": 0, "overridden": 0}
    assert db.commits == 1
    plan, tag_id, user_id, override = state["apply"]
    assert plan.switch == {B: {1}} and tag_id == 5 and user_id == "u1" and override == {}
    action, kwargs = state["audit"][0]
    assert action == "bulk_tag_by_query"
    assert kwargs["production_id"] == 1
    assert kwargs["details"]["conflict_action"] == "switch"
    assert kwargs["details"]["criteria"]["email_address"] == "jane@x.com"


def test_apply_leave_skips_conflicting(monkeypatch):
    _patch(monkeypatch, conflicts={B: {1}}, conflict_names={1: "Responsive"})
    out, _ = _run(_query_body(conflict_action="leave"), FakeTag())
    assert out["tagged"] == 1 and out["left"] == 1 and out["switched"] == 0


def test_readonly_403(monkeypatch):
    _patch(monkeypatch, roles={1: "readonly"})
    _raises(_query_body(), FakeTag(), 403)


def test_role_checked_for_every_production(monkeypatch):
    _patch(monkeypatch, matched={A: 1, B: 2}, roles={1: "reviewer", 2: "readonly"})
    _raises(_query_body(), FakeTag(), 403)


def test_override_requires_manager(monkeypatch):
    _patch(monkeypatch, roles={1: "reviewer"})
    priv = FakeTag(is_privilege=True, category="privilege", name="Attorney-Client")
    _raises(_query_body(disposition="withhold"), priv, 403)


def test_override_as_manager_passes_fields(monkeypatch):
    state = _patch(monkeypatch, roles={1: "manager"})
    priv = FakeTag(is_privilege=True, category="privilege", name="Attorney-Client")
    out, _ = _run(_query_body(disposition="withhold", description="  Counsel comms  "), priv)
    assert state["apply"][3] == {"privilege_disposition": "withhold",
                                 "privilege_description": "Counsel comms"}
    assert out["overridden"] == 2


def test_blank_description_is_not_an_override(monkeypatch):
    state = _patch(monkeypatch, roles={1: "reviewer"})
    priv = FakeTag(is_privilege=True, category="privilege")
    _run(_query_body(description="   "), priv)
    assert state["apply"][3] == {}


def test_tag_from_other_production_422(monkeypatch):
    _patch(monkeypatch, matched={A: 2})
    _raises(_query_body(), FakeTag(production_id=1), 422)


def test_explicit_ids_missing_404(monkeypatch):
    _patch(monkeypatch, matched={A: 1})
    _raises(BulkTagByQueryRequest(tag_id=5, doc_ids=[A, B]), FakeTag(), 404)


def test_explicit_ids_inaccessible_403(monkeypatch):
    _patch(monkeypatch, matched={A: 9}, accessible=(1,))
    _raises(BulkTagByQueryRequest(tag_id=5, doc_ids=[A]), FakeTag(), 403)


def test_attachments_off_skips_expansion(monkeypatch):
    state = _patch(monkeypatch, family={C: 1})
    out, _ = _run(_query_body(include_attachments=False, dry_run=True), FakeTag())
    assert state["expanded"] is False
    assert out["total"] == 2 and out["attachments_added"] == 0


def test_over_cap_422(monkeypatch):
    _patch(monkeypatch, family={C: 1})
    monkeypatch.setattr(rt.bt, "MAX_BULK_DOCS", 2)
    _raises(_query_body(dry_run=True), FakeTag(), 422)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bulk_tag_by_query_endpoint.py -q`
Expected: FAIL — `AttributeError: module 'app.routers.tags' has no attribute 'bt'`.

- [ ] **Step 3: Implement** — in `backend/app/routers/tags.py` add imports:

```python
from app.services import bulk_tagging as bt
from app.services.privilege import DISPOSITIONS
```

and add `BulkTagByQueryRequest` to the existing `from app.schemas import (...)` list. Then add after `bulk_tag`:

```python
@router.post("/documents/bulk-tag-by-query")
async def bulk_tag_by_query(
    body: BulkTagByQueryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Apply one tag to every document matching a search (or to explicit
    IDs), plus attachments. dry_run returns the counts the confirm step shows;
    conflicting responsiveness tags must be resolved with conflict_action."""
    tag = await db.get(Tag, body.tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    override: dict = {}
    if body.disposition is not None:
        if body.disposition not in DISPOSITIONS:
            raise HTTPException(status_code=422, detail="invalid disposition")
        override["privilege_disposition"] = body.disposition
    if body.description is not None and body.description.strip():
        override["privilege_description"] = body.description.strip()
    if override and not tag.is_privilege:
        raise HTTPException(
            status_code=422,
            detail="Privilege disposition and description only apply with a privilege tag")

    accessible = await get_accessible_production_ids(db, user)
    if body.query is not None:
        try:
            matched = await bt.resolve_query_docs(db, body.query, accessible)
        except bt.NoCriteriaError:
            raise HTTPException(status_code=422, detail="Search has no text or filters")
    else:
        matched = await bt.resolve_explicit_docs(db, body.doc_ids)
        missing = set(body.doc_ids) - set(matched)
        if missing:
            raise HTTPException(status_code=404, detail=f"Document {next(iter(missing))} not found")
        if any(pid not in accessible for pid in matched.values()):
            raise HTTPException(status_code=403, detail="Access denied")

    targets = await bt.expand_attachments(db, matched) if body.include_attachments else dict(matched)
    if len(targets) > bt.MAX_BULK_DOCS:
        raise HTTPException(
            status_code=422,
            detail=f"More than {bt.MAX_BULK_DOCS:,} documents — narrow the search")

    productions = set(targets.values())
    if tag.production_id is not None and productions - {tag.production_id}:
        raise HTTPException(status_code=422, detail="Tag belongs to a different production")
    required = "manager" if override else "reviewer"
    for pid in sorted(productions):
        role = await get_user_role_for_production(db, user, pid)
        if ROLE_RANK.get(role, 0) < ROLE_RANK[required]:
            raise HTTPException(
                status_code=403,
                detail="Manager or higher role required" if override else "Read-only access")

    ids = list(targets)
    already = await bt.already_tagged_ids(db, tag.id, ids)
    conflicts, conflict_names = await bt.conflicting_tags(db, tag, ids)
    plan = bt.plan_tagging(targets.keys(), already, conflicts, body.conflict_action)

    if body.dry_run:
        return {
            "matched": len(matched),
            "attachments_added": len(targets) - len(matched),
            "total": len(targets),
            "already_tagged": len(plan.already_tagged),
            "conflicts": len(plan.conflicts),
            "conflict_tags": bt.conflict_tag_counts(plan.conflicts, conflict_names),
        }
    if plan.conflicts and body.conflict_action is None:
        raise HTTPException(status_code=409, detail="conflicts_unresolved")

    overridden = await bt.apply_plan(db, plan, tag.id, user.id, override)

    criteria = (body.query.model_dump() if body.query is not None
                else {"doc_ids": [str(d) for d in body.doc_ids]})
    for pid in sorted(productions):
        in_prod = {d for d, p in targets.items() if p == pid}
        await log_action(
            db, user, "bulk_tag_by_query", "document", None,
            production_id=pid,
            details={
                "tag_id": tag.id,
                "tag_name": tag.name,
                "tagged": len(plan.to_tag & in_prod),
                "switched": len(set(plan.switch) & in_prod),
                "left": len(plan.left & in_prod),
                "conflict_action": body.conflict_action,
                "include_attachments": body.include_attachments,
                "criteria": criteria,
                "override": override,
            },
        )
    await db.commit()
    return {
        "total": len(targets),
        "tagged": len(plan.to_tag),
        "already_tagged": len(plan.already_tagged),
        "switched": len(plan.switch),
        "left": len(plan.left),
        "overridden": overridden,
    }
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_bulk_tag_by_query_endpoint.py -q` → all PASS.
Run: `python -m pytest -q` → all pass (includes `test_route_registration.py`).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/tags.py backend/tests/test_bulk_tag_by_query_endpoint.py
git commit -m "feat(tags): POST /documents/bulk-tag-by-query with dry run and conflict prompt"
```

---

### Task 4: Frontend — email + date filters, date sort

**Files:**
- Modify: `frontend/src/types/index.ts` (`Tag` at :6, `SearchResult` at :99; append new types)
- Modify: `frontend/src/api/client.ts` (`searchDocuments` ~:201; add bulk functions after `bulkTag` ~:240)
- Modify: `frontend/src/components/Omnibox.tsx`
- Modify: `frontend/src/components/AppHeader.tsx:13`
- Modify: `frontend/src/components/SearchResults.tsx` (result header row ~:54)
- Modify: `frontend/src/App.tsx` (`handleSearch` ~:200-240, re-run call sites, `clearSearch` ~:288, results header ~:620, browse sort select ~:802)

**Interfaces:**
- Produces (types): `EmailScope = 'from' | 'any'`; `EmailFilter { address: string; scope: EmailScope }`; `SearchExtras { email?: EmailFilter; dateFrom?: string; dateTo?: string }` (dates `YYYY-MM-DD`); `SearchSort = 'relevance' | 'bates' | 'date_asc' | 'date_desc'`; `BulkTagQuery`, `BulkTagByQueryBody`, `BulkTagDryRun`, `BulkTagApplied` (below); `Tag.is_privilege?: boolean`; `SearchResult.doc_date?: string | null`.
- Produces (client): `searchDocuments(..., sourceType?, extras?: SearchExtras)`; `bulkTagDryRun(body: Omit<BulkTagByQueryBody, 'dry_run'>): Promise<BulkTagDryRun>`; `bulkTagApply(body: Omit<BulkTagByQueryBody, 'dry_run'>): Promise<BulkTagApplied>`.
- Produces (App): state `lastExtras: SearchExtras | undefined`, `searchSort: SearchSort`; `handleSearch(query, metadata?, forceMode?, extras?)`; helper `hasExtras(extras?: SearchExtras): boolean`.

- [ ] **Step 1: Types** — in `frontend/src/types/index.ts`, add `is_privilege?: boolean;` to `interface Tag` after `keyboard_shortcut`; add `doc_date?: string | null;` to `interface SearchResult` after `rank`; and append:

```ts
export type EmailScope = 'from' | 'any';

export interface EmailFilter {
  address: string;
  scope: EmailScope;
}

/** Search filters beyond text + metadata. Dates are YYYY-MM-DD, inclusive. */
export interface SearchExtras {
  email?: EmailFilter;
  dateFrom?: string;
  dateTo?: string;
}

export type SearchSort = 'relevance' | 'bates' | 'date_asc' | 'date_desc';

/** The /api/search filters, so a bulk action resolves the same set. */
export interface BulkTagQuery {
  q: string;
  production_id: number;
  metadata?: Record<string, string> | null;
  file_type?: string | null;
  source_party?: string | null;
  source_type?: string | null;
  email_address?: string | null;
  email_scope?: EmailScope;
  date_from?: string | null;
  date_to?: string | null;
}

export interface BulkTagByQueryBody {
  doc_ids?: string[];
  query?: BulkTagQuery;
  tag_id: number;
  include_attachments: boolean;
  conflict_action?: 'switch' | 'leave' | null;
  disposition?: 'withhold' | 'redact_in_part' | 'produce' | null;
  description?: string | null;
  dry_run: boolean;
}

export interface BulkTagDryRun {
  matched: number;
  attachments_added: number;
  total: number;
  already_tagged: number;
  conflicts: number;
  conflict_tags: { tag_id: number; name: string; count: number }[];
}

export interface BulkTagApplied {
  total: number;
  tagged: number;
  already_tagged: number;
  switched: number;
  left: number;
  overridden: number;
}
```

- [ ] **Step 2: Client** — in `frontend/src/api/client.ts` add `SearchExtras, BulkTagByQueryBody, BulkTagDryRun, BulkTagApplied` to the existing `import type { ... } from '../types'` list. Add a final parameter to `searchDocuments`:

```ts
  sourceType?: string,
  extras?: SearchExtras,
): Promise<SearchResponse> {
```

and before its `return request<SearchResponse>(...)` line:

```ts
  if (extras?.email && extras.email.address.trim()) {
    params.set('email_address', extras.email.address.trim());
    params.set('email_scope', extras.email.scope);
  }
  if (extras?.dateFrom) params.set('date_from', extras.dateFrom);
  if (extras?.dateTo) params.set('date_to', extras.dateTo);
```

After `bulkTag`, add:

```ts
export const bulkTagDryRun = (body: Omit<BulkTagByQueryBody, 'dry_run'>) =>
  request<BulkTagDryRun>('/api/documents/bulk-tag-by-query', json({ ...body, dry_run: true }));

export const bulkTagApply = (body: Omit<BulkTagByQueryBody, 'dry_run'>) =>
  request<BulkTagApplied>('/api/documents/bulk-tag-by-query', json({ ...body, dry_run: false }));
```

- [ ] **Step 3: Omnibox** — in `frontend/src/components/Omnibox.tsx`:

Change the types import to `import type { EmailScope, SavedSearch, SearchExtras } from '../types';` and the prop to:

```ts
  onSearch: (query: string, metadata?: Record<string, string>, forceMode?: SearchMode, extras?: SearchExtras) => void;
```

Add state after `filterValue`:

```ts
  const [emailAddress, setEmailAddress] = useState('');
  const [emailScope, setEmailScope] = useState<EmailScope>('from');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
```

Replace `const filterCount = ...` and `submit` with:

```ts
  const metadataCount = Object.keys(metadataFilters).length;
  const emailActive = emailAddress.trim().length > 0;
  const filterCount = metadataCount + (emailActive ? 1 : 0) + (dateFrom || dateTo ? 1 : 0);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const extras: SearchExtras = {};
    if (emailActive) extras.email = { address: emailAddress.trim(), scope: emailScope };
    if (dateFrom) extras.dateFrom = dateFrom;
    if (dateTo) extras.dateTo = dateTo;
    const hasExtras = !!(extras.email || extras.dateFrom || extras.dateTo);
    if (!query.trim() && !hasExtras) return;
    if (dateFrom && dateTo && dateFrom > dateTo) {
      showToast('The "from" date is after the "to" date', 'error');
      return;
    }
    setMenu('none');
    // Email/date filters always run full-text server-side; say so up front so
    // the results header and bulk selection agree with what the server did.
    onSearch(query.trim(), metadataCount > 0 ? metadataFilters : undefined,
      hasExtras ? 'fulltext' : mode, hasExtras ? extras : undefined);
    setModeOverride(null);
  };
```

In the `menu === 'filters'` dropdown, insert as the first child (before the `Object.entries(metadataFilters)` map):

```tsx
          <form className="omnibox-save-row" onSubmit={submit} style={{ flexWrap: 'wrap' }}>
            <input
              className="input input-sm"
              placeholder="Email address or @domain"
              value={emailAddress}
              onChange={e => setEmailAddress(e.target.value)}
              aria-label="Email address filter"
              style={{ flex: '1 1 180px' }}
            />
            <div role="group" aria-label="Match the address in" style={{ display: 'flex', gap: 4 }}>
              <button
                type="button"
                className={`btn btn-xs ${emailScope === 'from' ? 'btn-primary' : 'btn-secondary'}`}
                aria-pressed={emailScope === 'from'}
                onClick={() => setEmailScope('from')}
              >Sender only</button>
              <button
                type="button"
                className={`btn btn-xs ${emailScope === 'any' ? 'btn-primary' : 'btn-secondary'}`}
                aria-pressed={emailScope === 'any'}
                onClick={() => setEmailScope('any')}
              >Any participant</button>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4, flex: '1 1 100%' }}>
              <label style={{ fontSize: 'var(--text-xs)' }} htmlFor="omnibox-date-from">From</label>
              <input id="omnibox-date-from" type="date" className="input input-sm"
                value={dateFrom} onChange={e => setDateFrom(e.target.value)} />
              <label style={{ fontSize: 'var(--text-xs)' }} htmlFor="omnibox-date-to">To</label>
              <input id="omnibox-date-to" type="date" className="input input-sm"
                value={dateTo} onChange={e => setDateTo(e.target.value)} />
            </div>
            {(emailActive || dateFrom || dateTo) && (
              <button type="button" className="btn btn-ghost btn-xs"
                onClick={() => { setEmailAddress(''); setDateFrom(''); setDateTo(''); }}>
                Clear
              </button>
            )}
            <button type="submit" className="btn btn-primary btn-xs">Search</button>
          </form>
```

- [ ] **Step 4: AppHeader prop type** — in `frontend/src/components/AppHeader.tsx:13`:

```ts
  onSearch?: (query: string, metadata?: Record<string, string>, forceMode?: SearchMode, extras?: SearchExtras) => void;
```

importing `SearchExtras` from `../types` (add `import type { SearchExtras } from '../types';` if the file has no types import).

- [ ] **Step 5: Result dates** — in `frontend/src/components/SearchResults.tsx`, change the meta span (`<span className="result-meta">{r.page_count} pg…</span>`) to:

```tsx
            <span className="result-meta">
              {r.doc_date ? `${r.doc_date.slice(0, 10)} · ` : ''}{r.page_count} pg{r.page_count !== 1 ? 's' : ''}
            </span>
```

- [ ] **Step 6: App wiring** — in `frontend/src/App.tsx`:

Add `SearchExtras, SearchSort` to the `./types` import. Next to `lastMetadata` (~:200):

```ts
  const [lastExtras, setLastExtras] = useState<SearchExtras | undefined>(undefined);
  const [searchSort, setSearchSort] = useState<SearchSort>('relevance');
```

Above `handleSearch`, add:

```ts
  const hasExtras = (x?: SearchExtras) => !!(x && (x.email || x.dateFrom || x.dateTo));
```

Change `handleSearch`'s signature to:

```ts
  const handleSearch = async (query: string, metadata?: Record<string, string>, forceMode?: SearchMode, extras?: SearchExtras) => {
```

inside it: after `setLastMetadata(metadata);` add `setLastExtras(extras);`; change the Bates lookup guard `if (!metadata && /^...$/.test(trimmed))` to `if (!metadata && !hasExtras(extras) && /^...$/.test(trimmed))`; change `const mode = forceMode ?? detectSearchMode(query);` to:

```ts
    const mode = hasExtras(extras) ? 'fulltext' : (forceMode ?? detectSearchMode(query));
```

and in the `searchDocuments(...)` call replace the hard-coded `'relevance'` with `searchSort` and append `extras` as the last argument (after `sourceTypeParam`).

Re-runs must keep the filters. Replace every `handleSearch(searchQuery, lastMetadata, lastSearchMode)` with `handleSearch(searchQuery, lastMetadata, lastSearchMode, lastExtras)` (verify all sites with `grep -n "handleSearch(searchQuery" src/App.tsx`), and the mode toggle button (~:633) with:

```tsx
                  onClick={() => handleSearch(searchQuery, lastMetadata, lastSearchMode === 'semantic' ? 'fulltext' : 'semantic', lastExtras)}
```

Add `searchSort` to the dependency array of the "Re-run the active search whenever the file-type filter changes" effect: `}, [filterFileType, filterSourceParty, workMode, searchSort]);`.

In `clearSearch` add `setLastExtras(undefined);`.

In the results header, right after `<span className="section-count">{searchTotal}</span>` (~:625), add:

```tsx
                {lastExtras?.email && (
                  <span className="badge badge-blue" style={{ marginLeft: 'var(--space-2)', fontWeight: 400 }}>
                    {lastExtras.email.scope === 'from' ? 'From' : 'Involving'} {lastExtras.email.address}
                  </span>
                )}
                {(lastExtras?.dateFrom || lastExtras?.dateTo) && (
                  <span className="badge badge-gray" style={{ marginLeft: 'var(--space-2)', fontWeight: 400 }}>
                    {lastExtras.dateFrom ?? '…'} – {lastExtras.dateTo ?? '…'}
                  </span>
                )}
```

In the same header's control group (right after the "Try full-text/semantic" button), add the search sort select:

```tsx
                <label htmlFor="search-sort" className="visually-hidden">Sort results</label>
                <select
                  id="search-sort"
                  className="input input-sm"
                  style={{ width: 'auto', minWidth: 100 }}
                  value={searchSort}
                  onChange={e => setSearchSort(e.target.value as SearchSort)}
                >
                  <option value="relevance">Sort: Relevance</option>
                  <option value="bates">Sort: Bates #</option>
                  <option value="date_asc">Sort: Date (oldest)</option>
                  <option value="date_desc">Sort: Date (newest)</option>
                </select>
```

In the browse list sort select (`id="sort-by"`, ~:802), add after the `size` option:

```tsx
                  <option value="date_asc">Sort: Date (oldest)</option>
                  <option value="date_desc">Sort: Date (newest)</option>
```

- [ ] **Step 7: Verify build + lint**

Run: `npm run build` → succeeds.
Before your first edit in this task, run `npx eslint src/components/Omnibox.tsx src/components/AppHeader.tsx src/components/SearchResults.tsx src/api/client.ts src/types/index.ts src/App.tsx` and record each file's error/warning count. Run it again now → no file exceeds its recorded count (`App.tsx` baseline is 7 errors / 1 warning).

- [ ] **Step 8: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/Omnibox.tsx frontend/src/components/AppHeader.tsx frontend/src/components/SearchResults.tsx frontend/src/App.tsx
git commit -m "feat(search-ui): email and date-range filters, date sort"
```

---

### Task 5: Frontend — select all matching + BulkTagDialog

**Files:**
- Create: `frontend/src/components/BulkTagDialog.tsx`
- Modify: `frontend/src/App.tsx` (state ~:70; `toggleSelect` ~:298; `handleSearch`/`clearSearch` resets; above `<SearchResults` ~:732; floating bar ~:1088)

**Interfaces:**
- Consumes: `bulkTagDryRun`, `bulkTagApply`, `BulkTagQuery`, `BulkTagByQueryBody`, `BulkTagDryRun`, `Tag`; App state `lastExtras`, `hasExtras()` (Task 4).
- Produces: `BulkTagDialog` default export, props `{ target: BulkTarget; tags: Tag[]; onClose: () => void; onApplied: (message: string) => void }`; exported type `BulkTarget = { kind: 'ids'; docIds: string[] } | { kind: 'query'; query: BulkTagQuery }`.

- [ ] **Step 1: Create `frontend/src/components/BulkTagDialog.tsx`**

```tsx
import { useRef, useState } from 'react';
import { bulkTagApply, bulkTagDryRun } from '../api/client';
import type { BulkTagByQueryBody, BulkTagDryRun, BulkTagQuery, Tag } from '../types';

export type BulkTarget =
  | { kind: 'ids'; docIds: string[] }
  | { kind: 'query'; query: BulkTagQuery };

type Disposition = '' | 'withhold' | 'redact_in_part' | 'produce';
type ConflictAction = 'switch' | 'leave';

interface Props {
  target: BulkTarget;
  tags: Tag[];
  onClose: () => void;
  onApplied: (message: string) => void;
}

const tagRank = (t: Tag) => (t.category === 'responsiveness' ? 0 : t.is_privilege ? 1 : 2);
const n = (v: number) => v.toLocaleString();

/**
 * Apply one tag to a selection or to every document matching the current
 * search. Always counts first (dry run) so the reviewer sees exactly what
 * will change — attachments pulled in, and documents that already carry a
 * conflicting responsiveness tag, which they must choose to switch or leave.
 */
export default function BulkTagDialog({ target, tags, onClose, onApplied }: Props) {
  const [tagId, setTagId] = useState<number | null>(null);
  const [includeAttachments, setIncludeAttachments] = useState(true);
  const [conflictAction, setConflictAction] = useState<ConflictAction | null>(null);
  const [disposition, setDisposition] = useState<Disposition>('');
  const [description, setDescription] = useState('');
  const [preview, setPreview] = useState<BulkTagDryRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Only the latest count may land: changing the tag mid-request must not
  // let a stale response overwrite the new one. Touched only in handlers.
  const requestSeq = useRef(0);

  const sortedTags = [...tags].sort((a, b) => tagRank(a) - tagRank(b) || a.name.localeCompare(b.name));
  const tag = tags.find(t => t.id === tagId) ?? null;

  const baseBody = (forTagId: number, withAttachments: boolean): Omit<BulkTagByQueryBody, 'dry_run'> => ({
    ...(target.kind === 'ids' ? { doc_ids: target.docIds } : { query: target.query }),
    tag_id: forTagId,
    include_attachments: withAttachments,
  });

  const runPreview = async (forTagId: number | null, withAttachments: boolean) => {
    const seq = ++requestSeq.current;
    setPreview(null);
    setError(null);
    setConflictAction(null);
    if (forTagId === null) return;
    try {
      const res = await bulkTagDryRun(baseBody(forTagId, withAttachments));
      if (seq === requestSeq.current) setPreview(res);
    } catch (e) {
      if (seq === requestSeq.current) setError(e instanceof Error ? e.message : 'Could not count documents');
    }
  };

  const apply = async () => {
    if (!tag || !preview || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await bulkTagApply({
        ...baseBody(tag.id, includeAttachments),
        conflict_action: preview.conflicts > 0 ? conflictAction : null,
        disposition: tag.is_privilege && disposition ? disposition : null,
        description: tag.is_privilege && description.trim() ? description.trim() : null,
      });
      const parts = [`Tagged ${n(res.tagged)} document${res.tagged === 1 ? '' : 's'} "${tag.name}"`];
      if (res.switched) parts.push(`${n(res.switched)} switched`);
      if (res.left) parts.push(`${n(res.left)} left as they were`);
      if (res.already_tagged) parts.push(`${n(res.already_tagged)} already tagged`);
      onApplied(parts.join(' · '));
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'unknown error';
      if (msg === 'conflicts_unresolved') {
        // Conflicts appeared since the count — recount and ask again.
        await runPreview(tag.id, includeAttachments);
      } else {
        setError(`Could not apply tag: ${msg}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const needsChoice = !!preview && preview.conflicts > 0 && conflictAction === null;
  const conflictNames = preview?.conflict_tags.map(c => c.name).join(' / ') ?? '';
  const applyCount = preview
    ? preview.total - (conflictAction === 'leave' ? preview.conflicts : 0)
    : 0;

  return (
    <div className="modal-overlay" onClick={() => { if (!busy) onClose(); }}>
      <div className="modal-panel" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="bulk-tag-title">
        <div className="modal-header">
          <h2 className="modal-title" id="bulk-tag-title">
            {target.kind === 'query' ? 'Tag all matching documents' : 'Tag selected documents'}
          </h2>
          <button className="modal-close-btn" aria-label="Close" onClick={onClose} disabled={busy}>&times;</button>
        </div>

        <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: 'var(--text-sm)', fontWeight: 600 }}>Tag</span>
            <select
              className="input"
              value={tagId ?? ''}
              onChange={e => {
                const next = e.target.value ? Number(e.target.value) : null;
                setTagId(next);
                runPreview(next, includeAttachments);
              }}
              disabled={busy}
            >
              <option value="">Choose a tag…</option>
              {sortedTags.map(t => (
                <option key={t.id} value={t.id}>{t.name}{t.is_privilege ? ' (privilege)' : ''}</option>
              ))}
            </select>
          </label>

          <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
            <input
              type="checkbox"
              checked={includeAttachments}
              onChange={e => {
                setIncludeAttachments(e.target.checked);
                runPreview(tagId, e.target.checked);
              }}
              disabled={busy}
            />
            Include attachments
          </label>

          {tagId !== null && !preview && !error && (
            <div style={{ fontSize: 'var(--text-sm)', color: 'rgba(44,62,107,0.6)' }}>Counting documents…</div>
          )}

          {preview && (
            <div style={{ fontSize: 'var(--text-sm)' }}>
              Apply to <strong>{n(preview.total)}</strong> document{preview.total === 1 ? '' : 's'}
              {' '}({n(preview.matched)} matched
              {includeAttachments ? ` + ${n(preview.attachments_added)} attachments` : ''}
              {preview.already_tagged ? `; ${n(preview.already_tagged)} already tagged` : ''})
            </div>
          )}

          {preview && preview.conflicts > 0 && tag && (
            <fieldset style={{ border: '1px solid var(--color-neutral-200)', borderRadius: 'var(--radius-sm)', padding: 'var(--space-2) var(--space-3)' }}>
              <legend style={{ fontSize: 'var(--text-sm)', padding: '0 4px' }}>
                <strong>{n(preview.conflicts)}</strong> of these {preview.conflicts === 1 ? 'is' : 'are'} currently tagged <em>{conflictNames}</em>.
              </legend>
              <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
                <input type="radio" name="conflict" checked={conflictAction === 'switch'}
                  onChange={() => setConflictAction('switch')} disabled={busy} />
                Switch them to {tag.name}
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
                <input type="radio" name="conflict" checked={conflictAction === 'leave'}
                  onChange={() => setConflictAction('leave')} disabled={busy} />
                Leave them as they are
              </label>
            </fieldset>
          )}

          {tag?.is_privilege && (
            <details>
              <summary style={{ fontSize: 'var(--text-sm)', cursor: 'pointer' }}>Privilege log options (managers only)</summary>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)', marginTop: 'var(--space-2)' }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--text-sm)' }}>
                  Disposition
                  <select className="input input-sm" value={disposition}
                    onChange={e => setDisposition(e.target.value as Disposition)} disabled={busy}>
                    <option value="">No override (withhold, from the tag)</option>
                    <option value="withhold">Withhold</option>
                    <option value="redact_in_part">Redact in part</option>
                    <option value="produce">Produce</option>
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--text-sm)' }}>
                  Log description (replaces the generated one on every document)
                  <textarea className="input" rows={3} value={description}
                    onChange={e => setDescription(e.target.value)} disabled={busy}
                    placeholder="e.g. Communication with outside counsel re: litigation strategy" />
                </label>
              </div>
            </details>
          )}

          {error && <div role="alert" style={{ color: 'var(--color-danger, #b42318)', fontSize: 'var(--text-sm)' }}>{error}</div>}
        </div>

        <div className="modal-footer" style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--space-2)' }}>
          <button className="btn btn-secondary btn-sm" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-primary btn-sm" onClick={apply} disabled={!preview || busy || needsChoice || applyCount === 0}>
            {busy ? 'Applying…' : preview ? `Apply to ${n(applyCount)} document${applyCount === 1 ? '' : 's'}` : 'Apply'}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Lint the new component**

Run: `npx eslint src/components/BulkTagDialog.tsx` → 0 errors. If `react-hooks/refs` flags `requestSeq`, it must only be touched inside `runPreview` (a handler-invoked function), never in render — keep it that way.

- [ ] **Step 3: App state + selection reset** — in `frontend/src/App.tsx`:

Import: `import BulkTagDialog, { type BulkTarget } from './components/BulkTagDialog';`

Add near `selectedIds` (~:70):

```ts
  // "Select all N matching": the selection becomes the search itself, so a
  // bulk action reaches every hit, not just the page on screen.
  const [allMatching, setAllMatching] = useState(false);
  const [showBulkDialog, setShowBulkDialog] = useState(false);
```

In `toggleSelect`, as the first line of the callback body: `setAllMatching(false);`. In `handleSearch` after `setSelectedIds(new Set());` and in `clearSearch` after its `setSelectedIds(new Set());`, add `setAllMatching(false);`.

Before the `return (` of `Home` (next to `const displayDocs = documents;`), add:

```ts
  // The server runs full-text whenever any filter is set (semantic ignores
  // filters). Only a full-text result set can be re-resolved as a query, so
  // "select all matching" is offered only then.
  const searchUsedFulltext = lastSearchMode === 'fulltext' || !!lastMetadata || hasExtras(lastExtras)
    || !!filterFileType || !!filterSourceParty || !!sourceTypeParam;
  const pageAllSelected = hasSearched && searchResults.length > 0
    && searchResults.every(r => selectedIds.has(r.id));
  const bulkTarget: BulkTarget = allMatching
    ? {
        kind: 'query',
        query: {
          q: searchQuery,
          production_id: production.id,
          metadata: lastMetadata ?? null,
          file_type: filterFileType || null,
          source_party: filterSourceParty || null,
          source_type: sourceTypeParam ?? null,
          email_address: lastExtras?.email?.address ?? null,
          email_scope: lastExtras?.email?.scope ?? 'from',
          date_from: lastExtras?.dateFrom ?? null,
          date_to: lastExtras?.dateTo ?? null,
        },
      }
    : { kind: 'ids', docIds: Array.from(selectedIds) };
  const selectedCount = allMatching ? searchTotal : selectedIds.size;
```

- [ ] **Step 4: Selection banner** — immediately before `<div className="card">\n              <SearchResults` (~:731), add:

```tsx
            {searchResults.length > 0 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: 'var(--space-2) 0', fontSize: 'var(--text-sm)' }}>
                {allMatching ? (
                  <>
                    <span>All <strong>{searchTotal.toLocaleString()}</strong> matching documents are selected.</span>
                    <button className="btn btn-ghost btn-xs" onClick={() => { setAllMatching(false); setSelectedIds(new Set()); }}>
                      Clear selection
                    </button>
                  </>
                ) : pageAllSelected ? (
                  <>
                    <span>All {searchResults.length} on this page are selected.</span>
                    {searchUsedFulltext && searchTotal > searchResults.length && (
                      <button className="btn btn-ghost btn-xs" onClick={() => setAllMatching(true)}>
                        Select all {searchTotal.toLocaleString()} matching
                      </button>
                    )}
                  </>
                ) : (
                  <button className="btn btn-ghost btn-xs" onClick={() => setSelectedIds(new Set(searchResults.map(r => r.id)))}>
                    Select all on this page
                  </button>
                )}
              </div>
            )}
```

- [ ] **Step 5: Floating bar** — in the floating bar (~:1088):

Replace `{selectedIds.size} selected` with `{selectedCount.toLocaleString()} selected`.

Wrap the existing Download button and the existing `<div style={{ position: 'relative' }}>…Tag picker…</div>` in `{!allMatching && (<>…</>)}` (both act on visible IDs only).

After that block, add:

```tsx
          <button
            className="btn btn-sm btn-primary"
            onClick={() => { setShowBulkTagPicker(false); setShowBulkDialog(true); }}
          >
            {allMatching ? 'Tag all matching…' : 'Tag with options…'}
          </button>
```

After the floating bar's closing `)}`, add:

```tsx
      {showBulkDialog && (
        <BulkTagDialog
          target={bulkTarget}
          tags={allTags}
          onClose={() => setShowBulkDialog(false)}
          onApplied={(message) => {
            setShowBulkDialog(false);
            setSelectedIds(new Set());
            setAllMatching(false);
            showToast(message, 'success');
            if (hasSearched) handleSearch(searchQuery, lastMetadata, lastSearchMode, lastExtras);
            else loadDocuments(docPage);
          }}
        />
      )}
```

- [ ] **Step 6: Verify build + lint**

Run: `npm run build` → succeeds.
Run: `npx eslint src/App.tsx src/components/BulkTagDialog.tsx` → `App.tsx` ≤ 7 errors / 1 warning; `BulkTagDialog.tsx` 0.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/BulkTagDialog.tsx frontend/src/App.tsx
git commit -m "feat(review-ui): select all matching + bulk tag dialog with conflict prompt"
```

---

### Task 6: End-to-end verification and PR

**Files:** none (verification only).

- [ ] **Step 1: Full backend suite** — `python -m pytest -q` from `backend/` → all pass (878 + new).

- [ ] **Step 2: Run locally** — per the local-dev setup: `docker compose -p descubre up -d db`; `python -m alembic upgrade head` (no new migration expected — confirm `git diff origin/main --stat -- backend/alembic` is empty); `python -m uvicorn app.main:app --port 8000` in `backend/`; `npm run dev` in `frontend/`. User signs in via the Playwright headed browser.

- [ ] **Step 3: Seed an email production** — ingest a small mbox (≥ 3 senders, ≥ 1 address with `_`, ≥ 1 email with attachments, ≥ 1 doc pre-tagged Responsive) into a local production (id ≥ 900001).

- [ ] **Step 4: Manual checks** (record results for the PR):
  1. Filters → email `@domain` + **Sender only** with empty query → only that domain's sent mail; header chip "From @domain".
  2. Same with **Any participant** → recipients/cc included; count ≥ sender-only.
  3. `j_smith@…` matches only that address (not `jxsmith@…`).
  4. Select page → "Select all N matching" appears (N > page) → floating bar shows N → **Tag all matching…** → pick **Not Responsive** → count shows matched + attachments, conflict prompt for the pre-tagged Responsive doc; Apply disabled until a choice; **Leave** → that doc still Responsive; repeat with **Switch** → now Not Responsive only.
  5. Pick a privilege tag, set description as a manager → privilege log CSV (`/api/productions/{id}/privilege-log/csv`) shows the description and basis for every tagged doc and its attachments.
  6. Semantic-mode query (e.g. "what did jane say") with no filters → no "Select all N matching" link (Review Focus #5).
  7. Uncheck "Include attachments" → count drops by the attachment count.
  8. Filters → From 2018-03-01 To 2018-03-31 (no text) → only March 2018 docs, including ones sent late on 3/31; header chip shows the range; a reversed range is refused with a toast.
  9. Search sort **Date (newest)** / **Date (oldest)** reorders results; result rows show the date; undated docs sort last. Browse list sort by date works too.
  10. Date range + `@domain` + **Tag all matching…** → dry-run count equals the search total (+ attachments).

- [ ] **Step 5: Push and open PR** (only after user approval to push)

```bash
git push -u origin feat/email-search-bulk-privilege
gh pr create --title "Email-address search + bulk tag by query (privilege, Not Responsive)" --body "<summary, manual check results, test counts>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```
