"""backfill typed metadata on existing documents from metadata_ (alias-only)

Revision ID: n6b1c3d95e02
Revises: m5a0b2c84d91
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "n6b1c3d95e02"
down_revision = "m5a0b2c84d91"
branch_labels = None
depends_on = None

_SET_COLUMNS = [
    "custodian", "date_sent", "date_received", "date_created", "date_modified",
    "file_hash_md5", "file_hash_sha256", "file_type", "file_name", "source_path",
    "email_from", "email_to", "email_cc", "email_bcc", "email_subject",
]

_BATCH_SIZE = 500


def upgrade() -> None:
    from app.services.metadata_normalize import backfill_typed_fields
    conn = op.get_bind()

    offset = 0
    while True:
        rows = conn.execute(
            sa.text("SELECT id, metadata FROM documents ORDER BY id LIMIT :limit OFFSET :offset"),
            {"limit": _BATCH_SIZE, "offset": offset},
        ).fetchall()
        if not rows:
            break

        for row in rows:
            meta = row._mapping["metadata"] or {}
            typed = backfill_typed_fields(meta)
            if not typed:
                continue
            # Only set columns currently NULL (idempotent; never overwrite).
            sets, params = [], {"id": row._mapping["id"]}
            for col in _SET_COLUMNS:
                if col in typed:
                    sets.append(f"{col} = COALESCE({col}, :{col})")
                    params[col] = typed[col]
            if sets:
                conn.execute(
                    sa.text(f"UPDATE documents SET {', '.join(sets)} WHERE id = :id"),
                    params,
                )

        offset += _BATCH_SIZE


def downgrade() -> None:
    pass  # data backfill; no structural change to reverse
