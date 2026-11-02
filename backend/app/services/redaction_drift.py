"""Has a locked set's redaction state moved since lock?

Render reads the documents' current redactions, but what was QC'd and
locked is the state at lock time. A set whose redactions were added,
edited or deleted after lock — or that has redactions on an item locked to
be produced unredacted — must not render; the fix is unlock and re-lock.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.models import Redaction


def drift_message(bates: list[str]) -> str:
    return f"Redactions changed after lock for: {', '.join(bates)} — unlock and lock again"


async def redaction_drift(db, ps, items) -> list[str]:
    """Bates begins of items whose redactions no longer match the lock."""
    if not items:
        return []
    doc_ids = [i.document_id for i in items]
    current = {doc: (n, latest) for doc, n, latest in (await db.execute(
        select(Redaction.document_id, func.count(Redaction.id),
               func.max(func.coalesce(Redaction.updated_at, Redaction.created_at)))
        .where(Redaction.document_id.in_(doc_ids))
        .group_by(Redaction.document_id)
    )).all()}
    locked_at = getattr(ps, "locked_at", None)
    drifted = []
    for item in items:
        n, latest = current.get(item.document_id, (0, None))
        snapshot = getattr(item, "redaction_count", None)
        produced_whole = item.disposition == "produce" or getattr(item, "produce_native", False)
        # A produce item may carry redactions only through an audited override
        # at lock (snapshot > 0); redactions it gained afterwards are drift.
        if n and produced_whole and not snapshot:
            drifted.append(item.bates_begin)
        elif getattr(item, "redaction_count", None) is not None and n != item.redaction_count:
            drifted.append(item.bates_begin)
        elif latest is not None and locked_at is not None and _naive(latest) >= _naive(locked_at):
            drifted.append(item.bates_begin)
    return drifted


def _naive(dt):
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
