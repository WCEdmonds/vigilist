"""Pure, DB-free email threading + inclusive derivation (SP4b-2).

Reused by both the async derive_threads service and the backfill migration, so
it must never touch the DB, the network, or global state. Deterministic and
order-independent: the same messages always produce the same thread_ids and the
same inclusive set regardless of input ordering.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document
from app.schemas import ThreadStats

logger = logging.getLogger(__name__)

_UPDATE_BATCH = 500

_PREFIX_RE = re.compile(r"^\s*(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)


def _safe_dt(dt: datetime | None) -> datetime:
    """Coerce a date to a comparable UTC-aware value (None → epoch, naive → UTC).

    Guards the latest-by-date fallback against a TypeError when a naive datetime
    is compared against the tz-aware epoch (Document.date_sent is tz-aware, but
    this keeps the pure engine robust for any caller / the backfill migration).
    """
    if dt is None:
        return _EPOCH
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
                if latest is None or _safe_dt(mm.date_sent) > _safe_dt(latest.date_sent):
                    latest = mm
            inclusive_ids = {latest.doc_id}

        for mm in members:
            result[mm.doc_id] = ThreadAssignment(
                thread_id=tid, is_inclusive=mm.doc_id in inclusive_ids
            )
    return result


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
                # Only derive over docs we own: our thread_ids are "T-…"; SP3
                # load-file thread_ids are never "T-…", so a load-file email
                # (whose "Type" column can also promote to file_type "email")
                # is left untouched. NULL threads are ours to fill; "T-…"
                # threads are ours to recompute.
                (Document.thread_id.is_(None)) | (Document.thread_id.like("T-%")),
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
