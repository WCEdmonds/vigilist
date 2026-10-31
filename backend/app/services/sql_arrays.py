"""Bind a set of document ids as ONE Postgres array parameter.

An expanding IN (...) binds one parameter per id, and asyncpg rejects any
statement with more than 32,767 parameters — a production-wide bulk action can
touch tens of thousands of documents.
"""

from sqlalchemy import any_, literal
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


def ids_match(column, ids):
    """`column = ANY(:ids)` with a single array bind parameter."""
    return column == any_(literal(list(ids), ARRAY(PG_UUID(as_uuid=True))))
