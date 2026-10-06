"""Decode RFC 2047 email headers stored raw before the parser fix (ops CLI).

Email documents ingested before ``decode_header_value`` existed carry headers
like '=?utf-8?Q?Mailchimp=20billing=20paused?='. This rewrites email_subject,
email_from/to/cc/bcc and the title (when it was derived from the raw subject)
for one production.

Usage (from backend/):

    venv/Scripts/python.exe -m scripts.backfill_email_headers --production-id 9 \\
        [--env-from-cloud-run vigilist-api --gcloud-account you@example.com] [--apply]

Without --apply it only reports what would change.
"""

import argparse
import asyncio

HEADER_FIELDS = ("email_subject", "email_from", "email_to", "email_cc", "email_bcc")


def decoded_updates(doc) -> dict:
    """Field -> decoded value for every header that decoding changes."""
    from app.services.email_parse import decode_header_value

    updates = {}
    for f in HEADER_FIELDS:
        raw = getattr(doc, f, None)
        if raw and "=?" in raw:
            new = decode_header_value(raw)
            if new != raw:
                updates[f] = new
    raw_subject = getattr(doc, "email_subject", None)
    if "email_subject" in updates and doc.title == (raw_subject or "")[:200]:
        updates["title"] = updates["email_subject"][:200] or None
    return updates


async def main() -> int:
    p = argparse.ArgumentParser(description="Decode raw RFC 2047 email headers in place")
    p.add_argument("--production-id", type=int, required=True)
    p.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    p.add_argument("--env-from-cloud-run", metavar="SERVICE")
    p.add_argument("--region", default="us-central1")
    p.add_argument("--project", default="ediscover")
    p.add_argument("--gcloud-account", default=None)
    args = p.parse_args()

    if args.env_from_cloud_run:
        from scripts.upload_ingest import _load_cloud_run_env
        _load_cloud_run_env(args.env_from_cloud_run, args.region, args.project, args.gcloud_account)

    from sqlalchemy import select

    from app.database import async_session
    from app.models import Document

    changed = 0
    async with async_session() as db:
        docs = (await db.execute(
            select(Document).where(
                Document.production_id == args.production_id,
                Document.file_type == "email",
            )
        )).scalars().all()
        for doc in docs:
            updates = decoded_updates(doc)
            if not updates:
                continue
            changed += 1
            if changed <= 5:
                print(f"{doc.bates_begin}: {updates.get('email_subject') or updates}")
            if args.apply:
                for k, v in updates.items():
                    setattr(doc, k, v)
        if args.apply:
            await db.commit()

    verb = "Updated" if args.apply else "Would update"
    print(f"{verb} {changed} of {len(docs)} email documents in production {args.production_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
