"""Search-term hit reports (P3-1). DB-aware; math in Python.

Standard meet-and-confer semantics per term: documents with hits,
family-expanded count (hits plus their family members), and unique hits
(documents no other term matches). One FTS query per term, one family-map
query, one corpus count — expansion and uniqueness computed from id sets.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document
from app.services.search import build_tsquery


def _scope_conditions(production_id: int, source_type: str | None) -> list:
    conditions = [Document.production_id == production_id]
    if source_type == "received":
        conditions.append(Document.source_type == "received")
    elif source_type == "collection":
        # NULL counts as ours — same semantics as the workspace toggle.
        conditions.append(Document.source_type.is_distinct_from("received"))
    return conditions


async def run_search_term_report(
    db: AsyncSession,
    production_id: int,
    terms: list[str],
    source_type: str | None = None,
) -> dict:
    scope = _scope_conditions(production_id, source_type)

    total_docs = (await db.execute(
        select(func.count(Document.id)).where(*scope)
    )).scalar() or 0

    # family_id -> set of doc ids, for expansion without per-term queries
    fam_rows = (await db.execute(
        select(Document.id, Document.family_id)
        .where(*scope, Document.family_id.is_not(None))
    )).all()
    family_members: dict[str, set] = {}
    for did, fam in fam_rows:
        family_members.setdefault(fam, set()).add(did)

    hits_by_term: dict[str, set] = {}
    fams_by_term: dict[str, set] = {}
    for term in terms:
        tsquery_str = build_tsquery(term)
        if not tsquery_str:
            hits_by_term[term] = set()
            fams_by_term[term] = set()
            continue
        tsquery = func.to_tsquery("english", tsquery_str)
        rows = (await db.execute(
            select(Document.id, Document.family_id)
            .where(*scope, Document.text_search_vector.op("@@")(tsquery))
        )).all()
        hits_by_term[term] = {r[0] for r in rows}
        fams_by_term[term] = {r[1] for r in rows if r[1]}

    # uniqueness: docs hit by exactly one term
    hit_counts: dict = {}
    for ids in hits_by_term.values():
        for did in ids:
            hit_counts[did] = hit_counts.get(did, 0) + 1

    term_rows = []
    any_hits: set = set()
    any_expanded: set = set()
    for term in terms:
        ids = hits_by_term[term]
        expanded = set(ids)
        for fam in fams_by_term[term]:
            expanded |= family_members.get(fam, set())
        term_rows.append({
            "term": term,
            "hits": len(ids),
            "with_families": len(expanded),
            "unique_hits": sum(1 for d in ids if hit_counts[d] == 1),
        })
        any_hits |= ids
        any_expanded |= expanded

    return {
        "total_docs": total_docs,
        "any_hits": len(any_hits),
        "any_with_families": len(any_expanded),
        "source_type": source_type,
        "terms": term_rows,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
