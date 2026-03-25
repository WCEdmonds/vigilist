"""Full-text search query builder and executor."""

import re

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document


def build_tsquery(user_query: str) -> str:
    """Convert a user search query to a PostgreSQL tsquery string.

    Supports:
    - Quoted phrases: "contract termination" -> phraseto_tsquery(...)
    - Boolean AND, OR, NOT
    - Wildcard *  -> :* prefix matching
    - Bare words joined with &
    """
    user_query = user_query.strip()
    if not user_query:
        return ""

    # Handle quoted phrases by converting to <-> (phrase operator)
    phrases = re.findall(r'"([^"]+)"', user_query)
    remaining = re.sub(r'"[^"]*"', " __PHRASE__ ", user_query)

    tokens = remaining.split()
    parts = []
    phrase_idx = 0

    for token in tokens:
        upper = token.upper()
        if token == "__PHRASE__":
            if phrase_idx < len(phrases):
                words = phrases[phrase_idx].strip().split()
                phrase_ts = " <-> ".join(w.lower() for w in words if w)
                parts.append(f"({phrase_ts})")
                phrase_idx += 1
        elif upper == "AND":
            parts.append("&")
        elif upper == "OR":
            parts.append("|")
        elif upper == "NOT":
            parts.append("!")
        elif token.endswith("*"):
            parts.append(f"{token[:-1].lower()}:*")
        else:
            parts.append(token.lower())

    # Join bare words with & if no explicit operator between them
    result = []
    for i, part in enumerate(parts):
        if i > 0 and part not in ("&", "|", "!") and result and result[-1] not in ("&", "|", "!"):
            result.append("&")
        result.append(part)

    return " ".join(result)


async def search_documents(
    db: AsyncSession,
    query: str,
    production_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
    sort: str = "relevance",
    accessible_production_ids: list[int] | None = None,
    metadata_filters: dict[str, str] | None = None,
) -> tuple[list[dict], int]:
    """Execute a full-text search and return results with snippets."""
    tsquery_str = build_tsquery(query) if query else ""
    has_text_query = bool(tsquery_str)

    if not has_text_query and not metadata_filters:
        return [], 0

    conditions = []
    if accessible_production_ids is not None:
        conditions.append(Document.production_id.in_(accessible_production_ids))
    if production_id is not None:
        conditions.append(Document.production_id == production_id)
    if has_text_query:
        tsquery = func.to_tsquery("english", literal_column(f"'{tsquery_str}'"))
        conditions.append(Document.text_search_vector.op("@@")(tsquery))
    if metadata_filters:
        for key, value in metadata_filters.items():
            if not re.match(r'^[a-zA-Z0-9_ ]+$', key):
                from fastapi import HTTPException
                raise HTTPException(status_code=400, detail=f"Invalid metadata key: {key}")
            conditions.append(Document.metadata_[key].astext.ilike(f"%{value}%"))

    if has_text_query:
        rank = func.ts_rank(Document.text_search_vector, tsquery).label("rank")
        headline = func.ts_headline(
            "english",
            func.coalesce(Document.text_content, literal_column("''")),
            tsquery,
            literal_column("'StartSel=<mark>, StopSel=</mark>, MaxWords=50, MinWords=20'"),
        ).label("snippet")
    else:
        rank = literal_column("0").label("rank")
        headline = func.substr(
            func.coalesce(Document.text_content, literal_column("''")), 1, 200
        ).label("snippet")

    # Count
    count_q = select(func.count(Document.id)).where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    # Results with snippets and rank
    q = (
        select(Document, rank, headline)
        .where(*conditions)
    )

    if sort == "bates":
        q = q.order_by(Document.bates_begin)
    else:
        q = q.order_by(rank.desc())

    q = q.offset((page - 1) * per_page).limit(per_page)
    rows = (await db.execute(q)).all()

    results = []
    for doc, rank_val, snippet in rows:
        results.append({
            "id": doc.id,
            "production_id": doc.production_id,
            "bates_begin": doc.bates_begin,
            "bates_end": doc.bates_end,
            "page_count": doc.page_count,
            "title": doc.title,
            "snippet": snippet,
            "rank": float(rank_val),
        })

    return results, total
