"""Pure production-set ordering + Bates numbering (P2-1). No DB/network.

Bates numbers are assigned at lock time from the set's own prefix; a
document's own bates_begin is an ingest control number, never reused here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

SORT_KEYS = frozenset({"control_number", "custodian_date"})


@dataclass(frozen=True)
class MemberInfo:
    document_id: Any
    control_number: str
    family_id: str | None
    custodian: str | None
    doc_date: datetime | None


def format_bates(prefix: str, number: int, padding: int) -> str:
    """SMITH + 1/6 -> SMITH000001. Wider numbers grow, never truncate."""
    return f"{prefix}{number:0{padding}d}"


def pages_for(disposition: str | None, page_count: int) -> int:
    """Withheld docs occupy exactly one page (the future slip-sheet)."""
    if disposition == "withhold":
        return 1
    return max(page_count, 1)


def _group_head_key(head: MemberInfo, sort_key: str):
    # isoformat strings compare like the datetimes they encode (single-corpus
    # timezones are uniform) and avoid naive/aware comparison errors that a
    # datetime sentinel for "missing" would introduce.
    if sort_key == "custodian_date":
        return (
            head.custodian or "",
            0 if head.doc_date is not None else 1,
            head.doc_date.isoformat() if head.doc_date is not None else "",
            head.control_number,
        )
    return (head.control_number,)


def order_members(members: list[MemberInfo], sort_key: str) -> list[MemberInfo]:
    """Families stay contiguous; groups interleave by the group head's key.

    Within a family, control-number order — parents are ingested before their
    attachments, so the parent sorts first and heads the group.
    """
    if sort_key not in SORT_KEYS:
        raise ValueError(f"unknown sort_key: {sort_key}")
    groups: dict[str, list[MemberInfo]] = {}
    for m in sorted(members, key=lambda m: m.control_number):
        key = m.family_id or f"\x00solo:{m.control_number}"
        groups.setdefault(key, []).append(m)
    ordered = sorted(groups.values(), key=lambda g: _group_head_key(g[0], sort_key))
    return [m for g in ordered for m in g]


def assign_bates(
    ordered: list[tuple[Any, int]], prefix: str, padding: int, start_number: int
) -> list[tuple[Any, int, str, str]]:
    """(doc_id, pages) in final order -> (doc_id, sort_order, begin, end).

    Gap-free: each doc starts where the previous ended + 1.
    """
    out = []
    n = start_number
    for i, (doc_id, pages) in enumerate(ordered, start=1):
        out.append((doc_id, i, format_bates(prefix, n, padding),
                    format_bates(prefix, n + pages - 1, padding)))
        n += pages
    return out
