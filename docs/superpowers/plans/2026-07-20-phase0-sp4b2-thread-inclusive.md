# Phase 0 SP4b-2 — Email Thread + Inclusive Derivation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Derive `thread_id` (conversation grouping) and `is_inclusive` (most-complete message) for parsed emails via a deterministic, production-wide post-pass.

**Architecture:** Capture RFC threading headers during SP4b-1 parsing and store them on the parent email Document (schema migration). A pure, DB-free engine (`email_threading.py`) groups a production's email Documents into threads (reply-chain union-find, normalized-subject fallback) and marks reply-graph leaves inclusive. A thin async service writes the results; it runs automatically when an ingest job finalizes, on demand via an endpoint, and once over existing data via a backfill migration.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Postgres (Neon), Python stdlib `email`, `extract-msg`. Backend tests run via `backend/venv/Scripts/python.exe -m pytest` from `backend/` (system python lacks deps).

## Global Constraints

- Three new **nullable** `Document` columns: `message_id` (String(500), indexed), `in_reply_to` (String(500)), `email_references` (Text). `references` is a SQL reserved word — the column is `email_references`.
- Threading headers are stored on the **parent** email Document only (attachments leave them null).
- `thread_id` is deterministic and order-independent: `"T-" + hashlib.sha1(f"{production_id}|{canonical_key}".encode("utf-8")).hexdigest()[:16]`, where `canonical_key` = the lexicographically-smallest member `message_id` for a header-formed thread, else `f"subj:{normalized_subject}"` (blank-subject orphans key on `f"doc:{doc_id}"`).
- `is_inclusive` = leaves of the reply graph (a message no other member replies to). For a thread with **no** reply links at all, only the most-recent message by `date_sent` is inclusive (tie → smallest `doc_id`).
- Derivation is scoped to `file_type == "email"` (SP4b-1's parent marker) so it never overwrites `thread_id`/`is_inclusive` SP3 set from a Relativity load file on other document types.
- The compute engine is pure/DB-free and reused by BOTH the async service and the sync backfill migration (no duplicated graph logic).
- `derive_threads`, the endpoint, and the auto-trigger are best-effort: a threading failure never fails/raises out of the ingest job or un-completes it.
- The SP4b-1 never-raise parser contract is preserved (`expand_email`/`_parse_*` never raise).
- Threads are scoped per production. Alembic single head before this work: `p7c2d4e06f13`.

---

## Task 1 — Capture threading headers in the parser (`email_parse.py`)

**Files:**
- Modify: `backend/app/services/email_parse.py` (`ParsedMessage` dataclass; `_parse_eml_bytes`; `_parse_msg_bytes`)
- Test: `backend/tests/test_email_parse.py` (add tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ParsedMessage` gains `message_id: str = ""`, `in_reply_to: str = ""`, `references: str = ""` (References collapsed to single-space-joined ids). Task 2's `build_email_documents` reads these.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_email_parse.py`:

```python
def _build_eml_with_threading() -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Re: Q3 numbers"
    msg["Date"] = "Mon, 20 Jul 2026 14:30:00 +0000"
    msg["Message-ID"] = "<msg-2@example.com>"
    msg["In-Reply-To"] = "<msg-1@example.com>"
    msg["References"] = "<msg-0@example.com>\r\n <msg-1@example.com>"
    msg.set_content("See below.")
    return msg.as_bytes()


def test_parse_eml_captures_threading_headers():
    parsed = _parse_eml_bytes(_build_eml_with_threading())
    assert parsed.message_id == "<msg-2@example.com>"
    assert parsed.in_reply_to == "<msg-1@example.com>"
    # References whitespace/newlines collapse to single-space-joined ids.
    assert parsed.references == "<msg-0@example.com> <msg-1@example.com>"


def test_parse_eml_without_threading_headers_yields_empty_strings():
    parsed = _parse_eml_bytes(_build_eml(with_attachment=False))
    assert parsed.message_id == ""
    assert parsed.in_reply_to == ""
    assert parsed.references == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `backend/`): `venv/Scripts/python.exe -m pytest tests/test_email_parse.py -k threading -v`
Expected: FAIL — `ParsedMessage` has no attribute `message_id` (or `TypeError` on unexpected kwarg).

- [ ] **Step 3: Add the dataclass fields**

In `backend/app/services/email_parse.py`, extend `ParsedMessage`:

```python
@dataclass
class ParsedMessage:
    from_: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    date_sent: str | None = None
    body_text: str = ""
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
```

- [ ] **Step 4: Populate them in `_parse_eml_bytes`**

In `_parse_eml_bytes`, change the `return ParsedMessage(...)` to also pass the headers (use the existing `_header` helper; collapse References whitespace):

```python
    return ParsedMessage(
        from_=_header(msg, "From"),
        to=_header(msg, "To"),
        cc=_header(msg, "Cc"),
        bcc=_header(msg, "Bcc"),
        subject=_header(msg, "Subject"),
        date_sent=_header(msg, "Date") or None,
        body_text=body_text,
        attachments=attachments,
        message_id=_header(msg, "Message-ID"),
        in_reply_to=_header(msg, "In-Reply-To"),
        references=" ".join(_header(msg, "References").split()),
    )
```

- [ ] **Step 5: Populate them in `_parse_msg_bytes`**

In `_parse_msg_bytes`, inside the `try:` after `msg = extract_msg.Message(tmp_path)`, read the headers from extract-msg. `msg.messageId` is the internet message id; `msg.header` is an `email.message.Message` of the transport headers (may be `None`). Guard everything:

```python
        message_id = getattr(msg, "messageId", "") or ""
        hdr = getattr(msg, "header", None)
        in_reply_to = (hdr.get("In-Reply-To") if hdr is not None else "") or ""
        references = (hdr.get("References") if hdr is not None else "") or ""
        return ParsedMessage(
            from_=(msg.sender or "").strip(),
            to=(msg.to or "").strip(),
            cc=(msg.cc or "").strip(),
            bcc=(msg.bcc or "").strip(),
            subject=(msg.subject or "").strip(),
            date_sent=(str(msg.date) if msg.date else None),
            body_text=(msg.body or "").strip(),
            attachments=attachments,
            message_id=message_id.strip(),
            in_reply_to=in_reply_to.strip(),
            references=" ".join(references.split()),
        )
```

(The rest of `_parse_msg_bytes` — temp-file write, `msg.close()`, `os.unlink` — is unchanged.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `venv/Scripts/python.exe -m pytest tests/test_email_parse.py -v`
Expected: PASS (all existing + 2 new; the `.msg` and `.pst` tests remain skipped).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/email_parse.py backend/tests/test_email_parse.py
git commit -m "feat(sp4b2): capture Message-ID/In-Reply-To/References in email parser"
```

---

## Task 2 — Schema columns + store headers on the parent Document

**Files:**
- Modify: `backend/app/models.py:113-115` (add three columns near `thread_id`)
- Create: `backend/alembic/versions/q8d3e5f17g24_add_email_threading_headers.py`
- Modify: `backend/app/services/ingest_native.py` (`build_email_documents` parent block)
- Test: migration verified against a throwaway Postgres (no pytest file)

**Interfaces:**
- Consumes: `ParsedMessage.message_id/in_reply_to/references` (Task 1).
- Produces: `Document.message_id`, `Document.in_reply_to`, `Document.email_references` columns; the parent email Document now carries them. Task 3/4/5 read them.

- [ ] **Step 1: Add the model columns**

In `backend/app/models.py`, immediately after the `is_inclusive` column (line 115), add:

```python
    is_inclusive = Column(Boolean, nullable=False, default=False)
    # Phase 0 SP4b-2 — email threading headers (parent email Documents only)
    message_id = Column(String(500), nullable=True, index=True)
    in_reply_to = Column(String(500), nullable=True)
    email_references = Column(Text, nullable=True)
```

(`String` and `Text` are already imported in `models.py`.)

- [ ] **Step 2: Write the schema migration**

Create `backend/alembic/versions/q8d3e5f17g24_add_email_threading_headers.py`:

```python
"""add email threading headers (message_id, in_reply_to, email_references)

Revision ID: q8d3e5f17g24
Revises: p7c2d4e06f13
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "q8d3e5f17g24"
down_revision = "p7c2d4e06f13"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("message_id", sa.String(length=500), nullable=True))
    op.add_column("documents", sa.Column("in_reply_to", sa.String(length=500), nullable=True))
    op.add_column("documents", sa.Column("email_references", sa.Text(), nullable=True))
    op.create_index("ix_documents_message_id", "documents", ["message_id"])


def downgrade():
    op.drop_index("ix_documents_message_id", table_name="documents")
    op.drop_column("documents", "email_references")
    op.drop_column("documents", "in_reply_to")
    op.drop_column("documents", "message_id")
```

- [ ] **Step 3: Store the headers on the parent in `build_email_documents`**

In `backend/app/services/ingest_native.py`, in the `parent = Document(...)` constructor inside `build_email_documents`, add the three fields (place them next to `email_subject`):

```python
        email_subject=(parsed.subject or None),
        date_sent=normalize_date(parsed.date_sent) if parsed.date_sent else None,
        message_id=(parsed.message_id or None),
        in_reply_to=(parsed.in_reply_to or None),
        email_references=(parsed.references or None),
    )
```

(Attachment child Documents are unchanged — they do not get these fields.)

- [ ] **Step 4: Verify the migration up/down against a throwaway Postgres**

Start a disposable pgvector Postgres and point Alembic at it:

```bash
docker run -d --rm --name sp4b2pg -e POSTGRES_USER=vigilist -e POSTGRES_PASSWORD=vigilist_dev -e POSTGRES_DB=vigilist -p 5433:5432 pgvector/pgvector:pg16
sleep 6
cd backend
VIGILIST_DATABASE_URL="postgresql+asyncpg://vigilist:vigilist_dev@localhost:5433/vigilist" venv/Scripts/python.exe -m alembic upgrade head
VIGILIST_DATABASE_URL="postgresql+asyncpg://vigilist:vigilist_dev@localhost:5433/vigilist" venv/Scripts/python.exe -m alembic downgrade -1
VIGILIST_DATABASE_URL="postgresql+asyncpg://vigilist:vigilist_dev@localhost:5433/vigilist" venv/Scripts/python.exe -m alembic upgrade head
cd ..
docker stop sp4b2pg
```

Expected: `upgrade head` reaches `q8d3e5f17g24`, `downgrade -1` drops the columns/index cleanly, re-`upgrade head` succeeds — no errors.

- [ ] **Step 5: Confirm the model imports cleanly**

Run (from `backend/`): `venv/Scripts/python.exe -c "import app.models; print('ok')"`
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/q8d3e5f17g24_add_email_threading_headers.py backend/app/services/ingest_native.py
git commit -m "feat(sp4b2): add email threading header columns + store on parent doc"
```

---

## Task 3 — Pure threading engine (`email_threading.py`)

**Files:**
- Create: `backend/app/services/email_threading.py`
- Test: `backend/tests/test_email_threading.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `normalize_subject(subject: str) -> str`
  - `@dataclass(frozen=True) ThreadMsg { doc_id: str, message_id: str = "", in_reply_to: str = "", references: str = "", subject: str = "", date_sent: datetime | None = None }`
  - `@dataclass(frozen=True) ThreadAssignment { thread_id: str, is_inclusive: bool }`
  - `compute_thread_assignments(messages: list[ThreadMsg], production_id: int) -> dict[str, ThreadAssignment]` (keyed by `doc_id`).
  Task 4 (`derive_threads`) and Task 5 (backfill migration) consume `ThreadMsg` + `compute_thread_assignments`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_email_threading.py`:

```python
"""Unit tests for the pure email-threading engine (SP4b-2). No DB/network."""

from datetime import datetime, timezone

from app.services.email_threading import (
    ThreadMsg,
    compute_thread_assignments,
    normalize_subject,
)


def _dt(day: int) -> datetime:
    return datetime(2026, 7, day, 12, 0, 0, tzinfo=timezone.utc)


def test_normalize_subject_strips_reply_forward_prefixes():
    assert normalize_subject("Re: Q3 numbers") == "q3 numbers"
    assert normalize_subject("FW: Fwd: Re:  Q3   numbers ") == "q3 numbers"
    assert normalize_subject("Q3 numbers") == "q3 numbers"


def test_reply_chain_groups_and_only_leaf_is_inclusive():
    msgs = [
        ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1)),
        ThreadMsg("b", message_id="<b>", in_reply_to="<a>", subject="Re: Hi", date_sent=_dt(2)),
        ThreadMsg("c", message_id="<c>", in_reply_to="<b>", subject="Re: Hi", date_sent=_dt(3)),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    tids = {res[k].thread_id for k in ("a", "b", "c")}
    assert len(tids) == 1  # one thread
    assert res["c"].is_inclusive is True
    assert res["a"].is_inclusive is False
    assert res["b"].is_inclusive is False


def test_branch_marks_both_leaves_inclusive():
    msgs = [
        ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1)),
        ThreadMsg("b", message_id="<b>", in_reply_to="<a>", subject="Re: Hi", date_sent=_dt(2)),
        ThreadMsg("c", message_id="<c>", in_reply_to="<a>", subject="Re: Hi", date_sent=_dt(3)),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    assert len({res[k].thread_id for k in ("a", "b", "c")}) == 1
    assert res["b"].is_inclusive is True
    assert res["c"].is_inclusive is True
    assert res["a"].is_inclusive is False


def test_references_only_linking_still_groups():
    msgs = [
        ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1)),
        ThreadMsg("b", message_id="<b>", references="<x> <a>", subject="Re: Hi", date_sent=_dt(2)),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    assert res["a"].thread_id == res["b"].thread_id
    assert res["b"].is_inclusive is True
    assert res["a"].is_inclusive is False


def test_subject_fallback_groups_headerless_and_latest_is_inclusive():
    msgs = [
        ThreadMsg("a", subject="Re: Budget", date_sent=_dt(1)),
        ThreadMsg("b", subject="Budget", date_sent=_dt(5)),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    assert res["a"].thread_id == res["b"].thread_id  # same normalized subject
    assert res["b"].is_inclusive is True   # latest by date
    assert res["a"].is_inclusive is False


def test_singleton_is_its_own_inclusive_thread():
    msgs = [ThreadMsg("solo", message_id="<solo>", subject="Unique", date_sent=_dt(1))]
    res = compute_thread_assignments(msgs, production_id=1)
    assert res["solo"].is_inclusive is True


def test_deterministic_regardless_of_input_order():
    base = [
        ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1)),
        ThreadMsg("b", message_id="<b>", in_reply_to="<a>", subject="Re: Hi", date_sent=_dt(2)),
        ThreadMsg("c", message_id="<c>", in_reply_to="<b>", subject="Re: Hi", date_sent=_dt(3)),
    ]
    forward = compute_thread_assignments(base, production_id=1)
    reversed_ = compute_thread_assignments(list(reversed(base)), production_id=1)
    assert forward == reversed_


def test_same_message_id_different_production_yields_different_thread_id():
    msgs = [ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1))]
    p1 = compute_thread_assignments(msgs, production_id=1)["a"].thread_id
    p2 = compute_thread_assignments(msgs, production_id=2)["a"].thread_id
    assert p1 != p2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/Scripts/python.exe -m pytest tests/test_email_threading.py -v`
Expected: FAIL — `No module named 'app.services.email_threading'`.

- [ ] **Step 3: Implement the engine**

Create `backend/app/services/email_threading.py`:

```python
"""Pure, DB-free email threading + inclusive derivation (SP4b-2).

Reused by both the async derive_threads service and the backfill migration, so
it must never touch the DB, the network, or global state. Deterministic and
order-independent: the same messages always produce the same thread_ids and the
same inclusive set regardless of input ordering.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_PREFIX_RE = re.compile(r"^\s*(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)


def normalize_subject(subject: str) -> str:
    """Lowercase; strip leading Re:/Fwd:/Fw: (repeatedly); collapse whitespace."""
    s = (subject or "").strip()
    while True:
        stripped = _PREFIX_RE.sub("", s, count=1)
        if stripped == s:
            break
        s = stripped
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass(frozen=True)
class ThreadMsg:
    doc_id: str
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    subject: str = ""
    date_sent: datetime | None = None


@dataclass(frozen=True)
class ThreadAssignment:
    thread_id: str
    is_inclusive: bool


def _thread_id(production_id: int, canonical_key: str) -> str:
    digest = hashlib.sha1(f"{production_id}|{canonical_key}".encode("utf-8")).hexdigest()
    return "T-" + digest[:16]


def _link_targets(msg: ThreadMsg) -> list[str]:
    targets: list[str] = []
    if msg.in_reply_to:
        targets.append(msg.in_reply_to.strip())
    for ref in msg.references.split():
        targets.append(ref.strip())
    return [t for t in targets if t]


class _UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Deterministic: smaller id becomes the root.
        lo, hi = (ra, rb) if ra <= rb else (rb, ra)
        self.parent[hi] = lo


def compute_thread_assignments(
    messages: list[ThreadMsg], production_id: int
) -> dict[str, ThreadAssignment]:
    """Group messages into threads and mark inclusive leaves. Keyed by doc_id."""
    if not messages:
        return {}

    by_doc = {m.doc_id: m for m in messages}
    # message_id -> doc_id (first by sorted doc_id wins; duplicates unioned below).
    msgid_to_doc: dict[str, str] = {}
    for m in sorted(messages, key=lambda x: x.doc_id):
        if m.message_id and m.message_id not in msgid_to_doc:
            msgid_to_doc[m.message_id] = m.doc_id

    uf = _UnionFind(by_doc.keys())

    # Union duplicate message_ids and resolved reply links.
    seen_msgid: dict[str, str] = {}
    for m in sorted(messages, key=lambda x: x.doc_id):
        if m.message_id:
            if m.message_id in seen_msgid:
                uf.union(m.doc_id, seen_msgid[m.message_id])
            else:
                seen_msgid[m.message_id] = m.doc_id
        for target in _link_targets(m):
            tgt_doc = msgid_to_doc.get(target)
            if tgt_doc is not None and tgt_doc != m.doc_id:
                uf.union(m.doc_id, tgt_doc)

    # replied_to: message_ids that some OTHER message links to (they have a child).
    replied_to: set[str] = set()
    for m in messages:
        for target in _link_targets(m):
            if target in msgid_to_doc and msgid_to_doc[target] != m.doc_id:
                replied_to.add(target)

    # Subject fallback: messages with no message_id AND no resolved link are
    # grouped by normalized subject (blank subjects stay singletons).
    def _is_orphan(m: ThreadMsg) -> bool:
        if m.message_id:
            return False
        return not any(t in msgid_to_doc for t in _link_targets(m))

    orphan_subject_root: dict[str, str] = {}
    for m in sorted(messages, key=lambda x: x.doc_id):
        if not _is_orphan(m):
            continue
        norm = normalize_subject(m.subject)
        if not norm:
            continue  # blank subject → its own singleton thread
        if norm in orphan_subject_root:
            uf.union(m.doc_id, orphan_subject_root[norm])
        else:
            orphan_subject_root[norm] = m.doc_id

    # Group docs by component root.
    components: dict[str, list[ThreadMsg]] = {}
    for m in messages:
        components.setdefault(uf.find(m.doc_id), []).append(m)

    result: dict[str, ThreadAssignment] = {}
    for members in components.values():
        member_msgids = sorted(mm.message_id for mm in members if mm.message_id)
        if member_msgids:
            canonical_key = member_msgids[0]
        else:
            norm = normalize_subject(members[0].subject)
            canonical_key = f"subj:{norm}" if norm else f"doc:{members[0].doc_id}"
        tid = _thread_id(production_id, canonical_key)

        has_links = any(mm.message_id in replied_to for mm in members)
        if has_links:
            inclusive_ids = {
                mm.doc_id for mm in members
                if not mm.message_id or mm.message_id not in replied_to
            }
        else:
            # No reply links at all → only the latest by date_sent (tie: smallest doc_id).
            latest = None
            for mm in sorted(members, key=lambda x: x.doc_id):
                if latest is None or (mm.date_sent or _EPOCH) > (latest.date_sent or _EPOCH):
                    latest = mm
            inclusive_ids = {latest.doc_id}

        for mm in members:
            result[mm.doc_id] = ThreadAssignment(
                thread_id=tid, is_inclusive=mm.doc_id in inclusive_ids
            )
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/Scripts/python.exe -m pytest tests/test_email_threading.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/email_threading.py backend/tests/test_email_threading.py
git commit -m "feat(sp4b2): pure email threading + inclusive engine"
```

---

## Task 4 — Async service, ingest trigger, and recompute endpoint

**Files:**
- Modify: `backend/app/services/email_threading.py` (add async `derive_threads` + `ThreadStats`)
- Modify: `backend/app/services/ingest.py` (`_finalize_job_if_done` — best-effort trigger)
- Modify: `backend/app/routers/intelligence.py` (add `POST /productions/{id}/rethread`)
- Modify: `backend/app/schemas.py` (add `ThreadStats`)
- Test: `backend/tests/test_email_threading.py` (add a `derive_threads` test over a fake session)

**Interfaces:**
- Consumes: `compute_thread_assignments`, `ThreadMsg` (Task 3); `Document` columns (Task 2); `get_accessible_production_ids`, `get_user_role_for_production`, `ROLE_RANK` (existing, `app.dependencies`).
- Produces: `async derive_threads(db, production_id) -> ThreadStats`; `ThreadStats` schema `{threads: int, inclusive: int, messages: int}`; endpoint `POST /api/productions/{production_id}/rethread`.

- [ ] **Step 1: Add the `ThreadStats` schema**

In `backend/app/schemas.py`, add (near the other output models):

```python
class ThreadStats(BaseModel):
    threads: int
    inclusive: int
    messages: int
```

- [ ] **Step 2: Write the failing `derive_threads` test**

Add to `backend/tests/test_email_threading.py`:

```python
def test_derive_threads_updates_docs_over_fake_session():
    import asyncio
    from types import SimpleNamespace

    from app.services.email_threading import derive_threads

    # Two parsed emails: b replies to a → one thread, b inclusive.
    rows = [
        SimpleNamespace(id="a", message_id="<a>", in_reply_to=None,
                        email_references=None, email_subject="Hi", date_sent=_dt(1)),
        SimpleNamespace(id="b", message_id="<b>", in_reply_to="<a>",
                        email_references=None, email_subject="Re: Hi", date_sent=_dt(2)),
    ]
    updates: list[dict] = []

    class FakeResult:
        def all(self_inner):
            return [(r.id, r.message_id, r.in_reply_to, r.email_references,
                     r.email_subject, r.date_sent) for r in rows]

    class FakeSession:
        async def execute(self_inner, stmt, params=None):
            if params is not None:            # the UPDATE calls carry params
                updates.append(params)
                return None
            return FakeResult()               # the SELECT call
        async def commit(self_inner):
            return None

    stats = asyncio.run(derive_threads(FakeSession(), production_id=1))
    assert stats.messages == 2
    assert stats.threads == 1
    assert stats.inclusive == 1
    by_id = {u["id"]: u for u in updates}
    assert by_id["b"]["inc"] is True
    assert by_id["a"]["inc"] is False
    assert by_id["a"]["tid"] == by_id["b"]["tid"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `venv/Scripts/python.exe -m pytest tests/test_email_threading.py -k derive_threads -v`
Expected: FAIL — `cannot import name 'derive_threads'`.

- [ ] **Step 4: Implement `derive_threads`**

Append to `backend/app/services/email_threading.py` (add the imports at the top of the file):

```python
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document
from app.schemas import ThreadStats

logger = logging.getLogger(__name__)

_UPDATE_BATCH = 500


async def derive_threads(db: "AsyncSession", production_id: int) -> ThreadStats:
    """Derive thread_id + is_inclusive for a production's parsed email Documents.

    Scoped to file_type == 'email' (SP4b-1 parent messages) so it never
    overwrites SP3 load-file thread_id on other document types. Idempotent and
    best-effort — logs and returns zeroed stats on failure rather than raising.
    """
    try:
        result = await db.execute(
            select(
                Document.id, Document.message_id, Document.in_reply_to,
                Document.email_references, Document.email_subject, Document.date_sent,
            ).where(
                Document.production_id == production_id,
                Document.file_type == "email",
            )
        )
        rows = result.all()
        if not rows:
            return ThreadStats(threads=0, inclusive=0, messages=0)

        messages = [
            ThreadMsg(
                doc_id=str(r[0]),
                message_id=r[1] or "",
                in_reply_to=r[2] or "",
                references=r[3] or "",
                subject=r[4] or "",
                date_sent=r[5],
            )
            for r in rows
        ]
        id_by_str = {str(r[0]): r[0] for r in rows}
        assignments = compute_thread_assignments(messages, production_id)

        pending = 0
        for doc_id_str, a in assignments.items():
            await db.execute(
                text("UPDATE documents SET thread_id = :tid, is_inclusive = :inc WHERE id = :id"),
                {"tid": a.thread_id, "inc": a.is_inclusive, "id": id_by_str[doc_id_str]},
            )
            pending += 1
            if pending >= _UPDATE_BATCH:
                await db.commit()
                pending = 0
        await db.commit()

        return ThreadStats(
            threads=len({a.thread_id for a in assignments.values()}),
            inclusive=sum(1 for a in assignments.values() if a.is_inclusive),
            messages=len(messages),
        )
    except Exception:
        logger.exception("derive_threads failed for production %s", production_id)
        return ThreadStats(threads=0, inclusive=0, messages=0)
```

The `SELECT` (a `select(...)` construct) is executed with no `params`, so the FakeSession returns `FakeResult`; each `UPDATE` (raw `text(...)`) carries a `params` dict, so the FakeSession records it. This mirrors `_persist_documents`' raw-SQL style and keeps the update observable to the test.

- [ ] **Step 5: Verify against the fake session**

Run: `venv/Scripts/python.exe -m pytest tests/test_email_threading.py -k derive_threads -v`
Expected: PASS.

- [ ] **Step 6: Wire the best-effort trigger into `_finalize_job_if_done`**

In `backend/app/services/ingest.py`, at the very end of `_finalize_job_if_done` (after the final `await db.commit()` on line 481), add:

```python
        await db.commit()

        # SP4b-2: derive email threads/inclusive for this production (best-effort;
        # a threading failure must never fail or un-complete the ingest job).
        try:
            from app.services.email_threading import derive_threads
            await derive_threads(db, production_id)
        except Exception:
            logger.exception("thread derivation skipped for production %s", production_id)
```

- [ ] **Step 7: Add the recompute endpoint**

In `backend/app/routers/intelligence.py`, add `ThreadStats` to the `app.schemas` import and add the endpoint (mirrors `detect_duplicates_endpoint`'s role check):

```python
@router.post("/productions/{production_id}/rethread", response_model=ThreadStats)
async def rethread_endpoint(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    from app.services.email_threading import derive_threads
    stats = await derive_threads(db, production_id)

    await log_action(db, user, "threads_derived", "production", str(production_id),
                     production_id=production_id, details=stats.model_dump())
    await db.commit()
    return stats
```

Update the import line at the top:

```python
from app.schemas import (
    ClusterOut, DuplicateEntryOut, FamilyMemberOut, FamilyThreadOut,
    PropagateTagRequest, ThreadStats,
)
```

- [ ] **Step 8: Run tests + import smokes**

Run (from `backend/`):
```bash
venv/Scripts/python.exe -m pytest tests/test_email_threading.py -v
venv/Scripts/python.exe -c "import app.services.ingest, app.routers.intelligence, app.services.email_threading; print('ok')"
```
Expected: all threading tests PASS; import prints `ok`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/email_threading.py backend/app/services/ingest.py backend/app/routers/intelligence.py backend/app/schemas.py backend/tests/test_email_threading.py
git commit -m "feat(sp4b2): derive_threads service + ingest trigger + rethread endpoint"
```

---

## Task 5 — Backfill data migration for already-ingested email

**Files:**
- Create: `backend/alembic/versions/r9e4f6g28h35_backfill_email_threads.py`
- Test: migration verified against a throwaway Postgres

**Interfaces:**
- Consumes: `compute_thread_assignments`, `ThreadMsg` (Task 3); the `documents` columns (Task 2).
- Produces: existing `file_type='email'` Documents get `thread_id`/`is_inclusive` (subject-fallback, since they predate header capture).

- [ ] **Step 1: Write the backfill migration**

Create `backend/alembic/versions/r9e4f6g28h35_backfill_email_threads.py`:

```python
"""backfill thread_id/is_inclusive for existing parsed email Documents

Revision ID: r9e4f6g28h35
Revises: q8d3e5f17g24
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "r9e4f6g28h35"
down_revision = "q8d3e5f17g24"
branch_labels = None
depends_on = None


def upgrade():
    from app.services.email_threading import ThreadMsg, compute_thread_assignments

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        prod_rows = conn.execute(sa.text(
            "SELECT DISTINCT production_id FROM documents WHERE file_type = 'email'"
        )).fetchall()
        for prod_row in prod_rows:
            production_id = prod_row._mapping["production_id"]
            rows = conn.execute(sa.text(
                "SELECT id, message_id, in_reply_to, email_references, email_subject, date_sent "
                "FROM documents WHERE production_id = :pid AND file_type = 'email'"
            ), {"pid": production_id}).fetchall()
            if not rows:
                continue
            messages = [
                ThreadMsg(
                    doc_id=str(r._mapping["id"]),
                    message_id=r._mapping["message_id"] or "",
                    in_reply_to=r._mapping["in_reply_to"] or "",
                    references=r._mapping["email_references"] or "",
                    subject=r._mapping["email_subject"] or "",
                    date_sent=r._mapping["date_sent"],
                )
                for r in rows
            ]
            id_by_str = {str(r._mapping["id"]): r._mapping["id"] for r in rows}
            assignments = compute_thread_assignments(messages, production_id)
            for doc_id_str, a in assignments.items():
                conn.execute(sa.text(
                    "UPDATE documents SET thread_id = :tid, is_inclusive = :inc WHERE id = :id"
                ), {"tid": a.thread_id, "inc": a.is_inclusive, "id": id_by_str[doc_id_str]})


def downgrade():
    # Data backfill; columns pre-exist (added in q8d3e5f17g24). No-op.
    pass
```

- [ ] **Step 2: Verify upgrade/downgrade + re-upgrade against a throwaway Postgres**

```bash
docker run -d --rm --name sp4b2pg2 -e POSTGRES_USER=vigilist -e POSTGRES_PASSWORD=vigilist_dev -e POSTGRES_DB=vigilist -p 5433:5432 pgvector/pgvector:pg16
sleep 6
cd backend
export VDB="postgresql+asyncpg://vigilist:vigilist_dev@localhost:5433/vigilist"
VIGILIST_DATABASE_URL="$VDB" venv/Scripts/python.exe -m alembic upgrade head
VIGILIST_DATABASE_URL="$VDB" venv/Scripts/python.exe -m alembic downgrade -1
VIGILIST_DATABASE_URL="$VDB" venv/Scripts/python.exe -m alembic upgrade head
cd ..
docker stop sp4b2pg2
```

Expected: `upgrade head` reaches `r9e4f6g28h35` and runs the backfill (empty DB → no rows, no error); `downgrade -1` is a clean no-op; re-`upgrade head` succeeds.

- [ ] **Step 3: Confirm single head**

Run (from `backend/`): `venv/Scripts/python.exe -m alembic heads`
Expected: exactly one line — `r9e4f6g28h35 (head)`.

- [ ] **Step 4: Commit**

```bash
git add backend/alembic/versions/r9e4f6g28h35_backfill_email_threads.py
git commit -m "feat(sp4b2): backfill thread_id/is_inclusive for existing parsed email"
```

---

## Task 6 — Full-suite verification

**Files:**
- No code changes (verification only). If a check fails, fix in the owning task's files.

- [ ] **Step 1: Run the full backend suite**

Run (from `backend/`): `venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all SP4b-2 tests pass; the only failure is the pre-existing, unrelated `tests/test_ai_review.py::test_build_classification_prompt` (untouched by this branch); `.msg`/`.pst` email tests remain skipped.

- [ ] **Step 2: Import smokes**

Run:
```bash
venv/Scripts/python.exe -c "import app.services.email_parse, app.services.email_threading, app.services.ingest_native, app.services.ingest, app.routers.intelligence, app.models; print('imports OK')"
```
Expected: `imports OK`.

- [ ] **Step 3: Confirm the migration chain is a single linear head**

Run (from `backend/`): `venv/Scripts/python.exe -m alembic heads`
Expected: exactly one head, `r9e4f6g28h35 (head)`.

- [ ] **Step 4: Record completion in the ledger and hand off to the whole-branch review.**

---

## Notes for the executor
- Backend tests MUST run via `backend/venv/Scripts/python.exe` (system python lacks `extract-msg`/`python-docx`/etc. and yields false failures).
- Never break the SP4b-1 never-raise contract (`expand_email`/`_parse_*`) or the atomic family persistence.
- `derive_threads`, the endpoint, and the trigger are best-effort — a threading failure must never fail the ingest job or the request beyond returning zeroed stats / logging.
- Migration revision ids (`q8d3e5f17g24`, `r9e4f6g28h35`) follow the existing `<rev>_<slug>.py` naming; if either collides with an existing file, pick the next unused id in the same style and update `down_revision` accordingly.
