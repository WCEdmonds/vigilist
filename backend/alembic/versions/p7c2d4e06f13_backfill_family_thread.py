"""backfill family_id/thread_id/is_inclusive from metadata_ (alias-only)

Revision ID: p7c2d4e06f13
Revises: n6b1c3d95e02
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "p7c2d4e06f13"
down_revision = "n6b1c3d95e02"
branch_labels = None
depends_on = None

_BATCH_SIZE = 500


def upgrade():
    from app.services.field_mapping import match_aliases
    from app.services.metadata_normalize import promote_record

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        offset = 0
        while True:
            rows = conn.execute(sa.text(
                "SELECT id, metadata FROM documents ORDER BY id LIMIT :lim OFFSET :off"
            ), {"lim": _BATCH_SIZE, "off": offset}).fetchall()
            if not rows:
                break
            for row in rows:
                meta = row._mapping["metadata"] or {}
                if not meta:
                    continue
                mapping = match_aliases(list(meta.keys()))
                # Only the three SP3 fields matter here.
                mapping = {k: v for k, v in mapping.items()
                           if k in ("family_id", "thread_id", "is_inclusive")}
                if not mapping:
                    continue
                typed, _ = promote_record(meta, mapping)
                if not typed:
                    continue
                # family_id/thread_id: fill only when currently NULL.
                # is_inclusive: NOT NULL default False — only set when the column
                #   resolved to True (never clobber with False).
                sets, params = [], {"id": row._mapping["id"]}
                if "family_id" in typed:
                    sets.append("family_id = COALESCE(family_id, :family_id)")
                    params["family_id"] = typed["family_id"]
                if "thread_id" in typed:
                    sets.append("thread_id = COALESCE(thread_id, :thread_id)")
                    params["thread_id"] = typed["thread_id"]
                if typed.get("is_inclusive") is True:
                    sets.append("is_inclusive = TRUE")
                if not sets:
                    continue
                conn.execute(sa.text(
                    f"UPDATE documents SET {', '.join(sets)} WHERE id = :id"
                ), params)
            offset += _BATCH_SIZE


def downgrade():
    # Data backfill; columns pre-exist, so downgrade is a no-op.
    pass
