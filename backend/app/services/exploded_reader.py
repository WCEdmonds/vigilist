"""Chunk-side reader over a PST exploded by the ``vigilist-pst-explode`` Job.

Mirrors ``pst_reader``'s ``iter_slice`` / ``item_kind`` / ``to_parsed`` so the
chunk worker's loop is unchanged; items come from the per-position JSON
objects (layout in ``pst_explode``) instead of the PST.

An item is lazy: its kind comes from the folder manifest (one GET per folder
in the slice), and its JSON and attachments are fetched only when it is
parsed, so unselected items in a type-filtered slice cost nothing.
"""

from __future__ import annotations

import json
from typing import Iterator

from app.services import storage
from app.services.email_parse import OversizedAttachment, ParsedMessage, UnreadableAttachment
from app.services.pst_explode import _PARSED_FIELDS, explode_prefix, item_object, manifest_object
from app.services.pst_reader import FolderSurvey, is_recoverable


class ExplodedItemError(Exception):
    """The item could not be read when the mailbox was unpacked."""


def _get_json(path: str) -> dict:
    return json.loads(storage.get_download_bytes(path))


class ExplodedItem:
    def __init__(self, prefix: str, folder_index: int, position: int, folder_path: str,
                 entry: dict | None):
        self.prefix = prefix
        self.folder_index = folder_index
        self.position = position
        self.folder_path = folder_path
        entry = entry or {"kind": None, "ok": False,
                          "error": "item is missing from the unpacked mailbox"}
        self.kind = entry.get("kind")
        self.ok = bool(entry.get("ok"))
        self.error = entry.get("error")
        self._data: dict | None = None

    @property
    def object_path(self) -> str:
        return item_object(self.prefix, self.folder_index, self.position)

    def data(self) -> dict:
        if not self.ok:
            raise ExplodedItemError(
                f"could not be read when the mailbox was unpacked: {self.error or 'unknown error'}")
        if self._data is None:
            self._data = _get_json(self.object_path)
        return self._data

    @property
    def subject(self) -> str:
        if not self.ok:
            return ""
        try:
            return (self.data().get("parsed") or {}).get("subject") or ""
        except Exception:
            return ""


def iter_slice(job_id: str, production_id: int, storage_path: str,
               slices: list[list[int]]) -> Iterator[tuple[int, FolderSurvey, int, ExplodedItem]]:
    prefix = explode_prefix(production_id, job_id, storage_path)
    manifests: dict[int, dict] = {}
    seq = 0
    for folder_index, start, end in slices:
        if folder_index not in manifests:
            manifests[folder_index] = _get_json(manifest_object(prefix, folder_index))
        manifest = manifests[folder_index]
        path = manifest.get("folder_path") or ""
        positions = manifest.get("positions") or {}
        info = FolderSurvey(folder_index, path, is_recoverable(path))
        for i in range(start, end):
            yield seq, info, i, ExplodedItem(prefix, folder_index, i, path, positions.get(str(i)))
            seq += 1


def item_kind(item: ExplodedItem) -> str:
    return item.kind or "other"


def _attachment(entry: dict) -> bytes:
    if entry.get("object"):
        return storage.get_download_bytes(entry["object"])
    over = entry.get("oversized")
    if over:
        return OversizedAttachment(int(over["size"]), int(over["limit"]))
    return UnreadableAttachment(entry.get("error") or "attachment could not be read")


def to_parsed(item: ExplodedItem, kind: str | None = None) -> ParsedMessage:
    """The item's ``ParsedMessage``, as ``pst_reader.to_parsed`` built it in
    the Job (``kind`` is the one recorded at explode time)."""
    data = item.data()
    fields = data.get("parsed") or {}
    return ParsedMessage(
        **{f: fields.get(f, None if f == "date_sent" else "") for f in _PARSED_FIELDS},
        attachments=[(a["name"], _attachment(a)) for a in data.get("attachments") or []],
        inline_names=set(fields.get("inline_names") or []),
        read_errors=list(data.get("read_errors") or []),
    )


def read_subject(job_id: str, production_id: int, storage_path: str,
                 folder_index: int, position: int) -> str:
    """Subject of one item: a single GET of its JSON."""
    prefix = explode_prefix(production_id, job_id, storage_path)
    data = _get_json(item_object(prefix, folder_index, position))
    return ((data.get("parsed") or {}).get("subject") or "").strip()
