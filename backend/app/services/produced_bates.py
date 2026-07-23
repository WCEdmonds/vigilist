"""Resolve PRODUCED Bates numbers back to documents (P2-5).

Documents carry ingest control numbers in bates_begin; the numbers WE
stamped live on production_set_items. When a citation or search references
a produced number ("SMITH000123"), find the locked set whose prefix matches
and the member whose range contains it.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProductionSet, ProductionSetItem
from app.services.production_numbering import format_bates

_BATES_RE = re.compile(r"^([A-Z]+)(\d+)$")


def split_bates(bates: str) -> tuple[str, int] | None:
    """Normalize (alnum-only, upper) and split into (alpha prefix, number)."""
    normalized = "".join(c for c in (bates or "") if c.isalnum()).upper()
    m = _BATES_RE.match(normalized)
    if not m:
        return None
    return m.group(1), int(m.group(2))


async def resolve_produced_bates(
    db: AsyncSession,
    accessible_ids: list[int],
    production_id: int | None,
    bates: str,
):
    """Return the document_id produced under this Bates number, or None.

    String range-compare is valid within one set because its padding is
    fixed; the query value is re-formatted with each candidate set's own
    prefix/padding first.
    """
    parsed = split_bates(bates)
    if not parsed:
        return None
    prefix_q, number = parsed

    scope = [production_id] if production_id else (accessible_ids or [])
    if not scope:
        return None

    sets = (await db.execute(
        select(ProductionSet).where(
            ProductionSet.status == "locked",
            ProductionSet.production_id.in_(scope),
            func.upper(
                func.regexp_replace(ProductionSet.prefix, "[^A-Za-z0-9]", "", "g")
            ) == prefix_q,
        )
    )).scalars().all()

    for ps in sets:
        q = format_bates(ps.prefix, number, ps.padding)
        doc_id = (await db.execute(
            select(ProductionSetItem.document_id).where(
                ProductionSetItem.production_set_id == ps.id,
                ProductionSetItem.bates_begin <= q,
                ProductionSetItem.bates_end >= q,
            ).limit(1)
        )).scalar_one_or_none()
        if doc_id is not None:
            return doc_id
    return None
