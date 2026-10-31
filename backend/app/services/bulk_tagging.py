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

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.models import Document, DocumentTag, Tag
from app.schemas import BulkTagQuery
from app.services.search import build_search_conditions
from app.services.sql_arrays import ids_match

# Categories where a document should carry at most one tag — applying one
# tag in the category conflicts with any other already on the document.
EXCLUSIVE_CATEGORIES = frozenset({"responsiveness"})
MAX_BULK_DOCS = 50_000


class NoCriteriaError(ValueError):
    """The query has no text or filters, so it would match nothing."""


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
        tz=query.tz,
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


async def existing_overrides(db, doc_ids) -> dict[UUID, tuple[str | None, str | None]]:
    """Docs that already carry a privilege disposition or description. These
    are usually written by hand, so a bulk override must not clobber them
    without the reviewer choosing to (and the old values being kept)."""
    if not doc_ids:
        return {}
    rows = (await db.execute(
        select(Document.id, Document.privilege_disposition, Document.privilege_description)
        .where(
            ids_match(Document.id, doc_ids),
            or_(Document.privilege_disposition.is_not(None),
                Document.privilege_description.is_not(None)),
        )
    )).all()
    return {doc_id: (disp, desc) for doc_id, disp, desc in rows}


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


async def apply_plan(db, plan: TagPlan, tag_id: int, user_id: str, override: dict,
                     override_ids=None) -> int:
    """Write the plan. The override goes to override_ids (default: every doc
    that ends up tagged). Returns how many documents got the override."""
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
    targets = plan.coded if override_ids is None else frozenset(override_ids)
    if override and targets:
        await db.execute(
            update(Document)
            .where(ids_match(Document.id, targets))
            .values(**override)
            .execution_options(synchronize_session=False)
        )
        return len(targets)
    return 0
