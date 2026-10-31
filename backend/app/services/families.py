"""Email families: link an attachment back to the email (or archive) it came from.

An attachment shares its parent's family_id, and the parent's family_id is the
parent's own control number ('ACME 000004 -0776' for the email,
'ACME 000004 -0776 .0001' for its first attachment).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, tuple_

from app.models import Document
from app.services.sql_arrays import ids_match


@dataclass(frozen=True)
class ParentRef:
    id: UUID
    bates_begin: str
    title: str
    is_email: bool


def is_attachment(bates_begin: str, family_id: str | None) -> bool:
    return bool(family_id) and family_id != bates_begin


def _parents_query(keys):
    return select(
        Document.id, Document.production_id, Document.bates_begin,
        Document.email_subject, Document.title, Document.file_type,
    ).where(tuple_(Document.production_id, Document.bates_begin).in_(list(keys)))


async def parent_refs(db, docs) -> dict[UUID, ParentRef]:
    """docs: (id, production_id, bates_begin, family_id) for one page of rows.
    Returns {attachment_id: ParentRef} for the rows that are attachments."""
    wanted = {doc_id: (prod_id, family_id) for doc_id, prod_id, bates, family_id in docs
              if is_attachment(bates, family_id)}
    if not wanted:
        return {}
    rows = (await db.execute(_parents_query(set(wanted.values())))).all()
    by_key = {
        (prod_id, bates): ParentRef(
            id=pid, bates_begin=bates,
            title=(subject or title or bates), is_email=(file_type == "email"),
        )
        for pid, prod_id, bates, subject, title, file_type in rows
    }
    return {doc_id: by_key[key] for doc_id, key in wanted.items() if key in by_key}


async def parent_refs_for_ids(db, ids) -> dict[UUID, ParentRef]:
    """Same as parent_refs, starting from bare document ids (one page of rows)."""
    ids = list(ids)
    if not ids:
        return {}
    rows = (await db.execute(
        select(Document.id, Document.production_id, Document.bates_begin, Document.family_id)
        .where(ids_match(Document.id, ids))
    )).all()
    return await parent_refs(db, rows)


def parent_fields(ref: ParentRef | None) -> dict:
    """Response fields for a document's parent (all None when it has none)."""
    if ref is None:
        return {"parent_id": None, "parent_bates": None, "parent_title": None, "parent_is_email": None}
    return {"parent_id": ref.id, "parent_bates": ref.bates_begin,
            "parent_title": ref.title, "parent_is_email": ref.is_email}
