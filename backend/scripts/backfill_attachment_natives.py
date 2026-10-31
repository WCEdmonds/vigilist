"""Store already-ingested email attachments so they preview (ops CLI).

Attachments ingested before attachment storage existed have native_path=NULL,
so the viewer shows "No viewable content". Their bytes still exist inside the
stored mailbox: this re-opens each parent's container (pst/ost/mbox/eml/msg,
downloaded once per container), finds the attachment whose SHA-256 matches the
one ingest recorded (position is unreliable: see attachment_index), uploads it to the
same path new ingests use, and sets native_path.

Usage (from backend/):

    venv/Scripts/python.exe -m scripts.backfill_attachment_natives --production-id 9 \\
        [--env-from-cloud-run vigilist-api --gcloud-account you@example.com] [--apply]

Without --apply it only reports what would change (still downloads containers
to verify matches). Attachments of emails that came out of a zip have no stored
container of their own and are reported, not changed.
"""

import argparse
import asyncio
import hashlib
import os


def attachment_index(messages) -> dict[str, tuple[str, bytes]]:
    """{sha256: (filename, bytes)} for every attachment in a re-expanded container.

    Attachments are found by their own hash, not their position: re-running
    readpst does not reproduce ingest's message order (on the real mailbox the
    positions drifted by a few messages), but an attachment's bytes don't change.
    """
    index: dict[str, tuple[str, bytes]] = {}
    for msg in messages:
        for name, data in msg.attachments:
            index.setdefault(hashlib.sha256(data).hexdigest(), (name, data))
    return index


def locate_attachment_bytes(index, expected_sha: str | None):
    """(filename, bytes) whose SHA-256 is what ingest recorded, else None —
    never store bytes we can't prove are the same file."""
    if not expected_sha:
        return None
    return index.get(expected_sha)


async def main() -> int:
    p = argparse.ArgumentParser(description="Store email attachments that were ingested without their file")
    p.add_argument("--production-id", type=int, required=True)
    p.add_argument("--apply", action="store_true", help="upload + write native_path (default: dry run)")
    p.add_argument("--env-from-cloud-run", metavar="SERVICE")
    p.add_argument("--region", default="us-central1")
    p.add_argument("--project", default="ediscover")
    p.add_argument("--gcloud-account", default=None)
    args = p.parse_args()

    if args.env_from_cloud_run:
        from scripts.upload_ingest import _load_cloud_run_env
        _load_cloud_run_env(args.env_from_cloud_run, args.region, args.project, args.gcloud_account)

    # Storage goes through firebase_admin, which the API initializes at
    # startup; a CLI has to do it itself.
    import firebase_admin

    from app.config import settings
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": settings.firebase_project_id or args.project})

    from sqlalchemy import select

    from app.database import async_session
    from app.models import Document
    from app.services.email_parse import expand_email
    from app.services.ingest_native import _store_attachment, attachment_storage_path
    from app.services.storage import get_download_bytes

    stats = {"stored": 0, "no_container": 0, "no_match": 0, "missing_parent": 0}
    async with async_session() as db:
        atts = (await db.execute(
            select(Document).where(
                Document.production_id == args.production_id,
                Document.native_path.is_(None),
                Document.family_id.is_not(None),
                Document.family_id != Document.bates_begin,
            )
        )).scalars().all()
        families = sorted({a.family_id for a in atts})
        parents = {
            d.bates_begin: d for d in (await db.execute(
                select(Document).where(
                    Document.production_id == args.production_id,
                    Document.bates_begin.in_(families),
                )
            )).scalars().all()
        }
        print(f"{len(atts)} attachments without a stored file across {len(families)} families")

        # Group by container so each mailbox is downloaded and parsed once.
        by_container: dict[str, list] = {}
        for a in atts:
            parent = parents.get(a.family_id)
            if parent is None:
                stats["missing_parent"] += 1
            elif not parent.native_path:
                stats["no_container"] += 1  # e.g. an email that came out of a zip
            else:
                by_container.setdefault(parent.native_path, []).append((parent, a))

        for container_path, pairs in by_container.items():
            data = await asyncio.to_thread(get_download_bytes, container_path)
            messages = await asyncio.to_thread(expand_email, os.path.basename(container_path), data)
            del data
            index = attachment_index(messages)
            print(f"  {container_path}: {len(messages)} messages re-expanded")
            del messages
            for parent, att in pairs:
                found = locate_attachment_bytes(index, att.file_hash_sha256)
                if found is None:
                    stats["no_match"] += 1
                    continue
                name, blob = found
                path = attachment_storage_path(args.production_id, att.bates_begin, att.file_name or name)
                if args.apply:
                    await asyncio.to_thread(_store_attachment, blob, path)
                    att.native_path = path
                stats["stored"] += 1
            if args.apply:
                await db.commit()  # per container: progress survives an interruption
            print(f"  {container_path}: {len(pairs)} attachments checked")

    verb = "Stored" if args.apply else "Would store"
    print(f"{verb} {stats['stored']}; no stored container {stats['no_container']}; "
          f"hash/position mismatch {stats['no_match']}; parent missing {stats['missing_parent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
