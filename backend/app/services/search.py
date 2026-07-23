"""Full-text search query builder and executor."""

import re

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document


# Characters allowed inside a tsquery lexeme. Everything else (quotes,
# tsquery operators, punctuation) is stripped so user input can never
# change the query structure or escape the string.
_LEXEME_STRIP_RE = re.compile(r"[^\w-]+", re.UNICODE)


def _sanitize_lexeme(word: str) -> str:
    """Reduce a word to a safe tsquery lexeme (letters/digits/_/- only)."""
    cleaned = _LEXEME_STRIP_RE.sub("", word).lower()
    # A lexeme must contain at least one word character — a bare "-" or
    # leftover punctuation would be a tsquery syntax error.
    if not re.search(r"\w", cleaned, re.UNICODE):
        return ""
    return cleaned


def build_tsquery(user_query: str) -> str:
    """Convert a user search query to a PostgreSQL tsquery string.

    Supports:
    - Quoted phrases: "contract termination" -> phrase (<->) matching
    - Boolean AND, OR, NOT
    - Wildcard *  -> :* prefix matching
    - Bare words joined with &

    All lexemes are sanitized and the output is assembled so it is always
    syntactically valid (no dangling operators), regardless of input.
    """
    user_query = user_query.strip()
    if not user_query:
        return ""

    # Handle quoted phrases by converting to <-> (phrase operator)
    phrases = re.findall(r'"([^"]+)"', user_query)
    remaining = re.sub(r'"[^"]*"', " __PHRASE__ ", user_query)

    # First pass: classify tokens into operands and operators
    items: list[tuple[str, str]] = []  # (kind, text); kind: operand|and|or|not
    phrase_idx = 0
    for token in remaining.split():
        upper = token.upper()
        if token == "__PHRASE__":
            if phrase_idx < len(phrases):
                words = [_sanitize_lexeme(w) for w in phrases[phrase_idx].split()]
                words = [w for w in words if w]
                phrase_idx += 1
                if words:
                    items.append(("operand", "(" + " <-> ".join(words) + ")"))
        elif upper == "AND":
            items.append(("and", "&"))
        elif upper == "OR":
            items.append(("or", "|"))
        elif upper == "NOT":
            items.append(("not", "!"))
        else:
            prefix = token.endswith("*")
            word = _sanitize_lexeme(token[:-1] if prefix else token)
            if word:
                items.append(("operand", f"{word}:*" if prefix else word))

    # Second pass: assemble a valid expression. Binary operators only ever
    # appear between operands (implicit AND when none was given); NOT only
    # ever prefixes an operand; dangling operators are dropped.
    out: list[str] = []
    pending_op: str | None = None
    pending_not = False
    for kind, txt in items:
        if kind == "operand":
            if out:
                out.append(pending_op or "&")
            out.append(f"!{txt}" if pending_not else txt)
            pending_op = None
            pending_not = False
        elif kind in ("and", "or"):
            if out:
                pending_op = txt
        else:  # not
            pending_not = True

    return " ".join(out)


FILE_TYPE_EXTENSIONS = {
    "video": [".mp4", ".mov", ".avi", ".webm"],
    "audio": [".wav", ".mp3"],
    "pdf": [".pdf"],
    "office": [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"],
    "image": [".png", ".jpg", ".jpeg", ".gif", ".bmp"],
    "email": [".msg", ".eml"],
}


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
) -> tuple[list[dict], int]:
    """Execute a full-text search and return results with snippets."""
    tsquery_str = build_tsquery(query) if query else ""
    has_text_query = bool(tsquery_str)

    if (not has_text_query and not metadata_filters and not file_type
            and not source_party and not source_type):
        return [], 0

    conditions = []
    if accessible_production_ids is not None:
        conditions.append(Document.production_id.in_(accessible_production_ids))
    if production_id is not None:
        conditions.append(Document.production_id == production_id)
    if has_text_query:
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
        from sqlalchemy import or_
        if file_type == "native":
            conditions.append(Document.native_path.isnot(None))
        elif file_type == "images_only":
            conditions.append(Document.native_path.is_(None))
        elif file_type in FILE_TYPE_EXTENSIONS:
            exts = FILE_TYPE_EXTENSIONS[file_type]
            conditions.append(or_(*[func.lower(Document.native_path).like(f"%{ext}") for ext in exts]))
    if source_party:
        conditions.append(Document.source_party == source_party)
    if source_type:
        conditions.append(Document.source_type == source_type)

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
