"""Full-text search query builder and executor."""

import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, literal_column, or_, select
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
    "email": [".msg", ".eml", ".mbox", ".pst", ".ost"],
}


def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input matches literally —
    email addresses routinely contain '_', which LIKE treats as a wildcard."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def email_address_condition(address: str, scope: str = "from"):
    """Case-insensitive substring match on the sender, or on any participant.

    The email_* columns hold raw header strings ('"Jane Doe" <jane@x.com>,
    ...'), so a substring match covers display-name forms and bare
    '@domain.com' alike.
    """
    pattern = f"%{escape_like(address.strip())}%"
    if scope == "any":
        cols = (Document.email_from, Document.email_to, Document.email_cc, Document.email_bcc)
    else:
        cols = (Document.email_from,)
    return or_(*(col.ilike(pattern, escape="\\") for col in cols))


def document_date():
    """A document's date: the sent date for emails, else the created date."""
    return func.coalesce(Document.date_sent, Document.date_created)


def date_order(direction: str) -> list:
    """ORDER BY document date with undated documents last, Bates as tiebreak."""
    d = document_date()
    return [(d.asc() if direction == "asc" else d.desc()).nulls_last(), Document.bates_begin]


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def build_search_conditions(
    query: str = "",
    *,
    production_id: int | None = None,
    accessible_production_ids: list[int] | None = None,
    metadata_filters: dict[str, str] | None = None,
    file_type: str | None = None,
    source_party: str | None = None,
    source_type: str | None = None,
    email_address: str | None = None,
    email_scope: str = "from",
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[list, object | None, bool]:
    """WHERE clauses for a search. Shared by /api/search and bulk
    tag-by-query so both resolve exactly the same document set.

    Returns (conditions, tsquery, has_criteria): tsquery is None without a
    text query; has_criteria False means the search would match nothing.
    Date bounds are inclusive whole days (UTC); undated documents never
    match a date bound.
    """
    tsquery_str = build_tsquery(query) if query else ""
    email = (email_address or "").strip()
    has_criteria = bool(tsquery_str or metadata_filters or file_type
                        or source_party or source_type or email
                        or date_from is not None or date_to is not None)

    conditions = []
    tsquery = None
    if accessible_production_ids is not None:
        conditions.append(Document.production_id.in_(accessible_production_ids))
    if production_id is not None:
        conditions.append(Document.production_id == production_id)
    if tsquery_str:
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
        if file_type == "native":
            conditions.append(Document.native_path.isnot(None))
        elif file_type == "images_only":
            conditions.append(Document.native_path.is_(None))
        elif file_type in FILE_TYPE_EXTENSIONS:
            exts = FILE_TYPE_EXTENSIONS[file_type]
            conditions.append(or_(*[func.lower(Document.native_path).like(f"%{ext}") for ext in exts]))
    if source_party:
        conditions.append(Document.source_party == source_party)
    if source_type == "received":
        conditions.append(Document.source_type == "received")
    elif source_type == "collection":
        # Undesignated (NULL) documents count as ours: only loads explicitly
        # marked as received are "incoming". Keeps the toggle meaningful on
        # matters ingested before source designation existed.
        conditions.append(Document.source_type.is_distinct_from("received"))
    if email:
        conditions.append(email_address_condition(email, email_scope))
    if date_from is not None:
        conditions.append(document_date() >= _day_start(date_from))
    if date_to is not None:
        conditions.append(document_date() < _day_start(date_to + timedelta(days=1)))

    return conditions, tsquery, has_criteria


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
    email_address: str | None = None,
    email_scope: str = "from",
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[list[dict], int]:
    """Execute a full-text search and return results with snippets."""
    conditions, tsquery, has_criteria = build_search_conditions(
        query,
        production_id=production_id,
        accessible_production_ids=accessible_production_ids,
        metadata_filters=metadata_filters,
        file_type=file_type,
        source_party=source_party,
        source_type=source_type,
        email_address=email_address,
        email_scope=email_scope,
        date_from=date_from,
        date_to=date_to,
    )
    if not has_criteria:
        return [], 0
    has_text_query = tsquery is not None

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
        select(Document, rank, headline, document_date().label("doc_date"))
        .where(*conditions)
    )

    if sort == "bates":
        q = q.order_by(Document.bates_begin)
    elif sort == "date_asc":
        q = q.order_by(*date_order("asc"))
    elif sort == "date_desc":
        q = q.order_by(*date_order("desc"))
    else:
        q = q.order_by(rank.desc())

    q = q.offset((page - 1) * per_page).limit(per_page)
    rows = (await db.execute(q)).all()

    results = []
    for doc, rank_val, snippet, doc_date in rows:
        results.append({
            "id": doc.id,
            "production_id": doc.production_id,
            "bates_begin": doc.bates_begin,
            "bates_end": doc.bates_end,
            "page_count": doc.page_count,
            "title": doc.title,
            "snippet": snippet,
            "rank": float(rank_val),
            "doc_date": doc_date,
        })

    return results, total
