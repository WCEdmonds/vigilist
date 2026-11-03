"""Unpack a large PST once: download, survey and explode it to Storage.

Runs only in the ``vigilist-pst-explode`` Cloud Run Job (8 vCPU, 32 GiB): the
PST is copied into in-memory /tmp with a sliced download, read locally with
pypff, and every item of every type is written to Storage keyed by its
traversal position, so chunk workers never open the PST:

- ``{prefix}/{folder_index:05d}/{position:07d}.json``: the item's
  ``ParsedMessage`` fields (no attachment bytes), kind, folder path,
  attachment list and read errors.
- ``{prefix}/{folder_index:05d}/{position:07d}.att{k}``: attachment ``k``
  (1-based, the attachment's order in the message) bytes.
- ``{prefix}/{folder_index:05d}/_manifest.json``:
  ``{"folder_path", "positions": {"<pos>": {"kind", "ok", "error"}}}``.
  ``ok`` is False for an item that could not be read at all (no item JSON);
  ``error`` carries its reason, or the read errors of a partly read item.

``prefix = productions/{pid}/exploded/{job_id}/{sha1(storage_path)[:12]}``.
The survey is built in the same walk, so its positions and the exploded
positions cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.services import pst_reader, storage
from app.services.email_parse import (
    OversizedAttachment,
    ParsedMessage,
    RawEmbeddedMessage,
    UnreadableAttachment,
)
from app.services.pst_reader import ContainerSurvey, SurveyBuilder, short_error

logger = logging.getLogger(__name__)

# Parallel uploads while exploding (pypff reads stay on one thread).
EXPLODE_UPLOAD_WORKERS = 32
# Upload back-pressure: the reader waits while this much is queued.
EXPLODE_MAX_PENDING_BYTES = 1024 * 1024 * 1024
EXPLODE_MAX_PENDING_OBJECTS = 4 * EXPLODE_UPLOAD_WORKERS
UPLOAD_ATTEMPTS = 4

# Must match the Job's --max-retries in backend-deploy.yml. 1 + retries runs =
# MAX_SURVEY_ATTEMPTS, so the counter and the last run agree.
EXPLODE_JOB_MAX_RETRIES = 2

# The PST is held in the Job's in-memory /tmp (32 GiB): larger ones are refused
# up front instead of failing after a long download.
MAX_UNPACK_BYTES = 28 * 1024 ** 3


class PermanentSurveyFailure(Exception):
    """A container that can never be unpacked (retrying will not help);
    ``line`` is the job error line."""

    def __init__(self, line: str):
        super().__init__(line)
        self.line = line


def too_large_line(name: str, size: int) -> str:
    return f"{name}: too large for in-memory unpack ({size / 1e9:.1f} GB); contact support"


class Progress:
    """Items handled so far (written by the explode thread, read by the
    Job's heartbeat)."""

    def __init__(self):
        self.value = 0

    def bump(self, n: int = 1) -> None:
        self.value += n


def container_key(storage_path: str) -> str:
    return hashlib.sha1(storage_path.encode("utf-8")).hexdigest()[:12]


def job_explode_prefix(production_id: int, job_id: str) -> str:
    """Everything one ingest job exploded (deleted at its post chunk)."""
    return f"productions/{production_id}/exploded/{job_id}/"


def explode_prefix(production_id: int, job_id: str, storage_path: str) -> str:
    return f"{job_explode_prefix(production_id, job_id)}{container_key(storage_path)}"


def item_object(prefix: str, folder_index: int, position: int) -> str:
    return f"{prefix}/{int(folder_index):05d}/{int(position):07d}.json"


def attachment_object(prefix: str, folder_index: int, position: int, k: int) -> str:
    return f"{prefix}/{int(folder_index):05d}/{int(position):07d}.att{int(k)}"


def manifest_object(prefix: str, folder_index: int) -> str:
    return f"{prefix}/{int(folder_index):05d}/_manifest.json"


_PARSED_FIELDS = ("from_", "to", "cc", "bcc", "subject", "date_sent", "body_text", "body_html",
                  "message_id", "in_reply_to", "references")


def serialize_item(parsed: ParsedMessage, kind: str, folder_path: str, prefix: str,
                   folder_index: int, position: int) -> tuple[bytes, list[tuple[str, bytes]]]:
    """(item JSON, [(attachment object, bytes)])."""
    attachments = []
    blobs: list[tuple[str, bytes]] = []
    for k, (name, data) in enumerate(parsed.attachments, start=1):
        entry = {"name": name, "inline": name in parsed.inline_names, "object": None, "error": None}
        if isinstance(data, OversizedAttachment):
            entry["error"] = f"attachment too large to load ({data.size:,} bytes; limit {data.limit:,})"
            entry["oversized"] = {"size": data.size, "limit": data.limit}
        elif isinstance(data, UnreadableAttachment):
            entry["error"] = data.error
        else:
            obj = attachment_object(prefix, folder_index, position, k)
            entry["object"] = obj
            if isinstance(data, RawEmbeddedMessage):
                entry["raw_note"] = data.note
            blobs.append((obj, bytes(data)))
        attachments.append(entry)
    fields = {f: getattr(parsed, f) for f in _PARSED_FIELDS}
    fields["inline_names"] = sorted(parsed.inline_names)
    doc = {
        "kind": kind,
        "folder_path": folder_path,
        "parsed": fields,
        "attachments": attachments,
        "read_errors": list(parsed.read_errors),
    }
    # ensure_ascii keeps lone surrogates (seen in PST strings) encodable.
    return json.dumps(doc).encode("ascii"), blobs


class _Uploader:
    """Bounded parallel uploads with retries. The first failure that
    survives its retries is raised from ``submit``/``close``."""

    def __init__(self, put, workers: int, retry_sleep: float):
        self._put = put
        self._retry_sleep = retry_sleep
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._cond = threading.Condition()
        self._bytes = 0
        self._objects = 0
        self._error: BaseException | None = None
        self.uploaded = 0

    def submit(self, path: str, data: bytes, content_type: str) -> None:
        size = len(data)
        with self._cond:
            while self._error is None and self._objects and (
                    self._objects >= EXPLODE_MAX_PENDING_OBJECTS
                    or self._bytes + size > EXPLODE_MAX_PENDING_BYTES):
                self._cond.wait()
            if self._error is not None:
                raise self._error
            self._bytes += size
            self._objects += 1
        self._pool.submit(self._run, path, data, content_type, size)

    def _run(self, path, data, content_type, size):
        try:
            for attempt in range(UPLOAD_ATTEMPTS):
                try:
                    self._put(path, data, content_type)
                    break
                except Exception:
                    if attempt == UPLOAD_ATTEMPTS - 1:
                        raise
                    time.sleep(self._retry_sleep * (2 ** attempt))
            with self._cond:
                self.uploaded += 1
        except BaseException as e:  # noqa: BLE001 - surfaced from submit/close
            logger.exception("Upload of %s failed", path)
            with self._cond:
                if self._error is None:
                    self._error = e
        finally:
            with self._cond:
                self._bytes -= size
                self._objects -= 1
                self._cond.notify_all()

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        if self._error is not None:
            raise self._error


def explode(handle, prefix: str, put, *, workers: int = EXPLODE_UPLOAD_WORKERS,
            retry_sleep: float = 1.0, progress: Progress | None = None) -> ContainerSurvey:
    """Write every item of the opened PST under ``prefix`` and return its
    survey. ``put(path, data, content_type)`` uploads one object."""
    builder = SurveyBuilder(getattr(handle, "storage_path", ""))
    up = _Uploader(put, workers, retry_sleep)
    items = partial = unreadable = 0
    try:
        for fi, (path, folder) in enumerate(pst_reader.folders(handle)):
            f = builder.folder(fi, path)
            positions: dict[str, dict] = {}
            for pos in range(folder.get_number_of_sub_messages()):
                items += 1
                if progress is not None:
                    progress.bump()
                try:
                    item = folder.get_sub_message(pos)
                except Exception as e:
                    # Counted at its position as "other" so later positions
                    # (and so plans and control numbers) do not shift.
                    builder.add(f, "other", 0)
                    positions[str(pos)] = {"kind": "other", "ok": False,
                                           "error": f"item could not be opened: {short_error(e)}"}
                    unreadable += 1
                    continue
                kind = pst_reader.item_kind(item)
                builder.add(f, kind, pst_reader._item_size(item))
                try:
                    parsed = pst_reader.to_parsed(item, kind)
                except Exception as e:
                    positions[str(pos)] = {"kind": kind, "ok": False,
                                           "error": f"item could not be read: {short_error(e)}"}
                    unreadable += 1
                    continue
                doc, blobs = serialize_item(parsed, kind, path, prefix, fi, pos)
                for obj, data in blobs:
                    up.submit(obj, data, "application/octet-stream")
                up.submit(item_object(prefix, fi, pos), doc, "application/json")
                errors = "; ".join(parsed.read_errors)[:2000] or None
                partial += 1 if errors else 0
                positions[str(pos)] = {"kind": kind, "ok": True, "error": errors}
            manifest = json.dumps({"folder_path": path, "positions": positions}).encode("ascii")
            up.submit(manifest_object(prefix, fi), manifest, "application/json")
    finally:
        up.close()
    logger.info("Exploded %s: %d items (%d partly read, %d unreadable), %d objects",
                prefix, items, partial, unreadable, up.uploaded)
    return builder.result


def unpack_container(production_id: int, job_id: str, storage_path: str,
                     tmp_dir: str | None = None, progress: Progress | None = None) -> dict:
    """Download, survey and explode one container (blocking; the Job's work).
    Returns the survey JSON, exactly as ``run_survey`` records it. Raises
    ``PermanentSurveyFailure`` for a container too large to unpack."""
    size = storage.get_size(storage_path)
    if size > MAX_UNPACK_BYTES:
        raise PermanentSurveyFailure(too_large_line(os.path.basename(storage_path), size))
    local = os.path.join(tmp_dir or tempfile.gettempdir(), f"explode-{container_key(storage_path)}.pst")
    started = time.monotonic()
    try:
        storage.download_sliced(storage_path, local)
        if progress is not None:
            progress.bump()
        logger.info("Downloaded %s in %.0f s", storage_path, time.monotonic() - started)
        prefix = explode_prefix(production_id, job_id, storage_path)
        with pst_reader.open_pst(local, storage_path) as handle:
            result = explode(handle, prefix,
                             lambda path, data, ct: storage.upload_bytes(data, path, content_type=ct),
                             progress=progress)
    finally:
        try:
            os.remove(local)
        except FileNotFoundError:
            pass
    result.storage_path = storage_path
    logger.info("Unpacked %s in %.0f s", storage_path, time.monotonic() - started)
    return result.to_json()


def delete_exploded(prefix: str) -> int:
    """Best-effort delete of an exploded prefix; returns objects deleted."""
    try:
        n = storage.delete_prefix(prefix)
        logger.info("Deleted %d exploded objects under %s", n, prefix)
        return n
    except Exception:
        logger.exception("Could not delete exploded objects under %s", prefix)
        return 0


# --- Job trigger --------------------------------------------------------------


def job_args(job_id: str, storage_path: str) -> list[str]:
    """Container args for one run (they replace the Job's default args)."""
    return ["-m", "app.jobs.explode_pst", "--job-id", str(job_id), "--storage-path", storage_path]


def _authorized_session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def trigger_explode_job(job_id: str, storage_path: str) -> None:
    """Start one run of the explode Job for a container (Cloud Run Admin API
    v2 ``jobs.run`` with container arg overrides)."""
    from app.services import tasks

    if not tasks.is_configured():
        raise RuntimeError("large mailbox loading requires Cloud Tasks")
    url = (f"https://run.googleapis.com/v2/projects/{settings.gcp_project_id}/locations/"
           f"{settings.gcp_location}/jobs/{settings.explode_job_name}:run")
    body = {"overrides": {"containerOverrides": [{"args": job_args(job_id, storage_path)}],
                          "taskCount": 1}}
    resp = _authorized_session().post(url, json=body, timeout=60)
    if resp.status_code >= 300:
        raise RuntimeError(f"mailbox unpack job could not be started (HTTP {resp.status_code}): "
                           f"{(resp.text or '')[:300]}")
    logger.info("Started %s for job %s: %s", settings.explode_job_name, job_id, storage_path)
