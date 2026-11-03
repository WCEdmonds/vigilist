"""Bind a set of ids (or other values) as ONE Postgres array parameter.

An expanding IN (...) binds one parameter per id, and asyncpg rejects any
statement with more than 32,767 parameters — a production-wide bulk action can
touch tens of thousands of documents.
"""

from sqlalchemy import String, any_, literal
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


def ids_match(column, ids):
    """`column = ANY(:ids)` with a single array bind parameter (UUID column)."""
    return column == any_(literal(list(ids), ARRAY(PG_UUID(as_uuid=True))))


# Batch size for id lists that must stay a plain IN (...): well under asyncpg's
# 32,767 bind-parameter cap.
IN_BATCH = 5_000


def batches(values, size: int = IN_BATCH):
    """Yield `values` as lists of at most `size` items."""
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def strings_match(column, values):
    """`column = ANY(:values)` with a single array bind parameter (text column)."""
    return column == any_(literal(list(values), ARRAY(String())))
