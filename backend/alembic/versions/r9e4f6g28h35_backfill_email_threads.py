"""backfill thread_id/is_inclusive for existing parsed email Documents

Revision ID: r9e4f6g28h35
Revises: q8d3e5f17g24
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "r9e4f6g28h35"
down_revision = "q8d3e5f17g24"
branch_labels = None
depends_on = None


def upgrade():
    from app.services.email_threading import ThreadMsg, compute_thread_assignments

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        # Only derive over docs we own: derived thread_ids are "T-…"; SP3
        # load-file thread_ids never are, so a load-file email (whose "Type"
        # column can also promote to file_type 'email') is left untouched.
        _SCOPE = "file_type = 'email' AND (thread_id IS NULL OR thread_id LIKE 'T-%')"
        prod_rows = conn.execute(sa.text(
            f"SELECT DISTINCT production_id FROM documents WHERE {_SCOPE}"
        )).fetchall()
        for prod_row in prod_rows:
            production_id = prod_row._mapping["production_id"]
            rows = conn.execute(sa.text(
                "SELECT id, message_id, in_reply_to, email_references, email_subject, date_sent "
                f"FROM documents WHERE production_id = :pid AND {_SCOPE}"
            ), {"pid": production_id}).fetchall()
            if not rows:
                continue
            messages = [
                ThreadMsg(
                    doc_id=str(r._mapping["id"]),
                    message_id=r._mapping["message_id"] or "",
                    in_reply_to=r._mapping["in_reply_to"] or "",
                    references=r._mapping["email_references"] or "",
                    subject=r._mapping["email_subject"] or "",
                    date_sent=r._mapping["date_sent"],
                )
                for r in rows
            ]
            id_by_str = {str(r._mapping["id"]): r._mapping["id"] for r in rows}
            assignments = compute_thread_assignments(messages, production_id)
            for doc_id_str, a in assignments.items():
                conn.execute(sa.text(
                    "UPDATE documents SET thread_id = :tid, is_inclusive = :inc WHERE id = :id"
                ), {"tid": a.thread_id, "inc": a.is_inclusive, "id": id_by_str[doc_id_str]})


def downgrade():
    # Data backfill; columns pre-exist (added in q8d3e5f17g24). No-op.
    pass
