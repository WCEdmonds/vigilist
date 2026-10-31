"""Load the messages an email container holds that ingest never did (ops CLI).

Until readpst ran as a single job (-j 0), its parallel mode silently dropped a
varying share of each PST's messages — about 3.5% of one real mailbox. This
re-expands each stored PST/OST/mbox of a production, works out which messages
are already loaded, and ingests only the rest, numbered after the container's
existing messages (BASE -2326, -2327, ...). Existing documents and their coding
are never touched.

A message counts as already loaded when a loaded message of the same container
has the same Message-ID, else the same sent date and body text, else the same
serialized hash (each loaded document matches once). The serialized hash alone
is not enough: it includes the subject, whose header decoding changed after
the early loads.

Usage (from backend/, or as the Cloud Run job's command):

    python -m scripts.topup_container_messages --production-id 12 [--apply]

Without --apply it only reports. With --apply it commits per container, then
runs embeddings and email threading and starts the ambient pipeline, as the
end of a normal ingest does.
"""

import argparse
import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime

from app.services.email_parse import ParsedMessage

_CONTAINER_EXTS = (".pst", ".ost", ".mbox")
_MESSAGE_CONTROL = re.compile(r"^(.*) -(\d{4,})$")


@dataclass(frozen=True)
class ExistingMessage:
    message_id: str | None
    date_sent: datetime | None
    body_hash: str | None
    file_hash: str | None


def body_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8", errors="replace")).hexdigest()


def existing_from_row(row) -> ExistingMessage:
    """(message_id, date_sent, text_content, file_hash_sha256) -> ExistingMessage."""
    message_id, date_sent, text, file_hash = row
    return ExistingMessage(message_id or None, date_sent, body_hash(text), file_hash)


def _keys(message_id, date_sent, body, file_hash) -> list[tuple]:
    keys = []
    if message_id:
        keys.append(("id", message_id))
    if date_sent is not None and body is not None:
        keys.append(("date+body", date_sent, body))
    if file_hash:
        keys.append(("hash", file_hash))
    return keys


def find_missing(messages: list[ParsedMessage], existing: list[ExistingMessage]) -> list[ParsedMessage]:
    """The re-expanded messages no loaded document accounts for."""
    from app.services.ingest_native import _serialize_message
    from app.services.metadata_normalize import normalize_date

    pools: dict[tuple, list[int]] = {}
    for i, e in enumerate(existing):
        for key in _keys(e.message_id, e.date_sent, e.body_hash, e.file_hash):
            pools.setdefault(key, []).append(i)
    used: set[int] = set()
    missing = []
    for m in messages:
        keys = _keys(
            m.message_id or None,
            normalize_date(m.date_sent) if m.date_sent else None,
            body_hash(m.body_text),
            hashlib.sha256(_serialize_message(m)).hexdigest(),
        )
        match = next((i for key in keys for i in pools.get(key, ()) if i not in used), None)
        if match is None:
            missing.append(m)
        else:
            used.add(match)
    return missing


def container_base(bates: list[str]) -> tuple[str, int]:
    """('BASE', next message number) from a container's message control numbers."""
    bases, numbers = set(), []
    for b in bates:
        m = _MESSAGE_CONTROL.match(b)
        if not m:
            raise ValueError(f"not a multi-message control number: {b!r}")
        bases.add(m.group(1))
        numbers.append(int(m.group(2)))
    if len(bases) != 1:
        raise ValueError(f"container spans several control bases: {sorted(bases)}")
    return bases.pop(), max(numbers) + 1


async def main() -> int:
    p = argparse.ArgumentParser(description="Load email container messages that ingest dropped")
    p.add_argument("--production-id", type=int, required=True)
    p.add_argument("--apply", action="store_true", help="ingest the missing messages (default: report only)")
    args = p.parse_args()

    # Storage goes through firebase_admin, which the API initializes at
    # startup; a CLI has to do it itself.
    import firebase_admin

    from app.config import settings
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": settings.firebase_project_id})

    from sqlalchemy import select

    from app.database import async_session
    from app.models import Document
    from app.services.email_parse import expand_email
    from app.services.ingest_native import _serialize_message, _store_attachment, build_email_documents
    from app.services.ingest_pdf import _ocr_jpeg_text
    from app.services.storage import get_download_bytes

    pid = args.production_id
    total_missing = 0
    async with async_session() as db:
        containers = sorted(
            path for (path,) in (await db.execute(
                select(Document.native_path).where(
                    Document.production_id == pid, Document.file_type == "email",
                    Document.native_path.is_not(None),
                ).distinct()
            )).all()
            if path.lower().endswith(_CONTAINER_EXTS)
        )
        print(f"{len(containers)} email containers in production {pid}")

        for path in containers:
            loaded = (await db.execute(
                select(Document).where(
                    Document.production_id == pid, Document.file_type == "email",
                    Document.native_path == path,
                )
            )).scalars().all()
            existing = [existing_from_row((d.message_id, d.date_sent, d.text_content, d.file_hash_sha256))
                        for d in loaded]
            try:
                base, next_m = container_base([d.bates_begin for d in loaded])
            except ValueError as e:
                print(f"  {path}: skipped ({e})")
                continue
            template = loaded[0]
            # Release the connection before the minutes-long readpst + parse.
            await db.commit()

            data = await asyncio.to_thread(get_download_bytes, path)
            messages = await asyncio.to_thread(expand_email, os.path.basename(path), data)
            del data
            missing = find_missing(messages, existing)
            total_missing += len(missing)
            print(f"  {path}: {len(messages)} messages in container, {len(loaded)} loaded, "
                  f"{len(missing)} missing (new numbers {base} -{next_m:04d} on)")
            if not args.apply or not missing:
                continue

            docs = []
            for offset, parsed in enumerate(missing):
                control = f"{base} -{next_m + offset:04d}"
                family = await asyncio.to_thread(
                    build_email_documents,
                    parsed,
                    message_control=control,
                    production_id=pid,
                    source_path=template.source_path,
                    custodian=template.custodian,
                    msg_bytes=_serialize_message(parsed),
                    native_path=path,
                    ocr_fn=_ocr_jpeg_text,
                    store_fn=_store_attachment,
                )
                for d in family:
                    d.source_party = template.source_party
                    d.source_type = template.source_type
                    d.metadata_ = {**(d.metadata_ or {}), "Loaded By": "PST top-up (readpst -j 0)"}
                docs.extend(family)
            db.add_all(docs)
            await db.commit()  # per container: progress survives an interruption
            print(f"    added {len(missing)} messages, {len(docs) - len(missing)} attachments")

        print(f"{'Added' if args.apply else 'Would add'} {total_missing} messages")
        if not (args.apply and total_missing):
            return 0

        # The end of a normal ingest: embeddings, threads, ambient pipeline.
        from app.services import tasks as task_service
        from app.services.email_threading import derive_threads
        from app.services.embeddings import embed_production_documents

        await embed_production_documents(db, pid)
        await derive_threads(db, pid)
        await db.commit()
        if task_service.is_configured():
            task_service.enqueue_pipeline(pid)
            print("Embeddings + threads done; ambient pipeline enqueued")
        else:
            print("Embeddings + threads done; Cloud Tasks not configured, pipeline NOT started")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
