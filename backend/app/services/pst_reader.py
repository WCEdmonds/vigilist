"""Stream a large PST/OST straight from object storage with pypff.

No whole-container download and no /tmp writes: pypff reads through a seekable
GCS reader. The survey walks the folder tree once (counts + bytes by item
type); `iter_slice` later yields exactly the `[folder, start, end)` ranges a
chunk owns, so chunks are deterministic and independent.

pypff is imported lazily inside `open_pst` so everything else here is testable
with a fake item tree.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import Iterator

from app.services import storage
from app.services.email_parse import ParsedMessage, _strip_html, decode_header_value

READER_MODE = "gcs_stream"
GCS_READ_CHUNK_BYTES = 8 * 1024 * 1024  # provisional, pending throughput spike

ITEM_TYPES = ("email", "calendar", "contact", "task", "note", "other")

_CLASS_PREFIXES = (
    ("ipm.note", "email"),
    ("ipm.schedule.meeting", "email"),
    ("ipm.appointment", "calendar"),
    ("ipm.contact", "contact"),
    ("ipm.distlist", "contact"),
    ("ipm.task", "task"),
    ("ipm.stickynote", "note"),
)


def item_type(message_class: str | None) -> str:
    if not message_class:
        return "email"
    mc = message_class.strip().lower()
    for prefix, kind in _CLASS_PREFIXES:
        if mc.startswith(prefix):
            return kind
    return "other"


def is_recoverable(folder_path: str) -> bool:
    return any(
        seg.strip().lower() in ("recoverable items", "deleted items")
        for seg in folder_path.split("/")
    )


@dataclass
class FolderSurvey:
    index: int
    path: str
    recoverable: bool
    counts: dict[str, int] = field(default_factory=dict)
    bytes: dict[str, int] = field(default_factory=dict)


@dataclass
class ContainerSurvey:
    storage_path: str
    folders: list[FolderSurvey] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        out = {t: 0 for t in ITEM_TYPES}
        for f in self.folders:
            for t, n in f.counts.items():
                out[t] = out.get(t, 0) + n
        return out

    def to_json(self) -> dict:
        return {
            "storage_path": self.storage_path,
            "folders": [
                {
                    "index": f.index,
                    "path": f.path,
                    "recoverable": f.recoverable,
                    "counts": f.counts,
                    "bytes": f.bytes,
                }
                for f in self.folders
            ],
        }

    @classmethod
    def from_json(cls, data: dict) -> "ContainerSurvey":
        return cls(
            storage_path=data["storage_path"],
            folders=[FolderSurvey(**f) for f in data.get("folders", [])],
        )


class PstHandle:
    """Wrapper around an opened pypff file exposing its root folder."""

    def __init__(self, root, storage_path: str = ""):
        self.root = root
        self.storage_path = storage_path

    def get_root_folder(self):
        return self.root


@contextlib.contextmanager
def open_pst(storage_path: str):
    """Open ``storage_path`` via a seekable GCS stream (READER_MODE gcs_stream)."""
    import pypff  # lazy: only the worker image needs it

    reader = storage.open_blob_reader(storage_path, GCS_READ_CHUNK_BYTES)
    pst = pypff.file()
    try:
        pst.open_file_object(reader)
        yield PstHandle(pst.get_root_folder(), storage_path)
    finally:
        try:
            pst.close()
        finally:
            reader.close()


def _root(handle):
    return handle.get_root_folder() if hasattr(handle, "get_root_folder") else handle


def _walk(folder, path: str, out: list[tuple[str, object]]) -> None:
    out.append((path, folder))
    for i in range(folder.get_number_of_sub_folders()):
        child = folder.get_sub_folder(i)
        name = getattr(child, "name", None) or f"folder{i}"
        _walk(child, f"{path}/{name}" if path else name, out)


def _folders(handle) -> list[tuple[str, object]]:
    root = _root(handle)
    out: list[tuple[str, object]] = []
    _walk(root, getattr(root, "name", None) or "", out)
    return out


# MAPI property tags (non-named, so readable straight from record sets).
PR_MESSAGE_CLASS = 0x001A
PR_MESSAGE_SIZE = 0x0E08
PR_RECIPIENT_TYPE = 0x0C15
PR_DISPLAY_NAME = 0x3001
PR_EMAIL_ADDRESS = 0x3003
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_CONTENT_ID = 0x3712
PR_COMPANY_NAME = 0x3A16
PR_BUSINESS_PHONE = 0x3A08
PR_MOBILE_PHONE = 0x3A1C
PR_SMTP_ADDRESS = 0x39FE


def _prop(obj, tag: int):
    """First record entry with MAPI property ``tag`` across obj.record_sets."""
    try:
        record_sets = list(obj.record_sets or [])
    except Exception:
        return None
    for rs in record_sets:
        try:
            entry = rs.get_entry_by_type(tag)
        except Exception:
            entry = None
        if entry is not None:
            return entry
    return None


def _prop_str(obj, tag: int) -> str:
    entry = _prop(obj, tag)
    if entry is None:
        return ""
    try:
        return _text(entry.data_as_string).strip()
    except Exception:
        return ""


def _prop_int(obj, tag: int) -> int | None:
    entry = _prop(obj, tag)
    if entry is None:
        return None
    try:
        v = entry.data_as_integer
    except Exception:
        return None
    return int(v) if v is not None else None


def _item_size(item) -> int:
    return _prop_int(item, PR_MESSAGE_SIZE) or 0


def survey(handle) -> ContainerSurvey:
    result = ContainerSurvey(storage_path=getattr(handle, "storage_path", ""))
    for idx, (path, folder) in enumerate(_folders(handle)):
        counts = {t: 0 for t in ITEM_TYPES}
        sizes = {t: 0 for t in ITEM_TYPES}
        for i in range(folder.get_number_of_sub_messages()):
            item = folder.get_sub_message(i)
            kind = item_type(_prop_str(item, PR_MESSAGE_CLASS) or None)
            counts[kind] += 1
            sizes[kind] += _item_size(item)
        result.folders.append(FolderSurvey(idx, path, is_recoverable(path), counts, sizes))
    return result


def iter_slice(handle, slices: list[list[int]]) -> Iterator[tuple[int, FolderSurvey, int, object]]:
    folders = _folders(handle)
    seq = 0
    for folder_index, start, end in slices:
        path, folder = folders[folder_index]
        info = FolderSurvey(folder_index, path, is_recoverable(path))
        for i in range(start, end):
            yield seq, info, i, folder.get_sub_message(i)
            seq += 1


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _headers(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current = None
    for line in raw.splitlines():
        if line[:1] in (" ", "\t") and current:
            out[current] += " " + line.strip()
        elif ":" in line:
            name, _, val = line.partition(":")
            current = name.strip().lower()
            if current in out:
                current = None  # keep the first occurrence only
            else:
                out[current] = val.strip()
        else:
            current = None
    return out


def _attachments(item) -> list:
    n = getattr(item, "number_of_attachments", 0) or 0
    return [item.get_attachment(i) for i in range(n)]


def _attachment_name(att) -> str:
    return (
        _text(getattr(att, "long_filename", None)).strip()
        or _prop_str(att, PR_ATTACH_FILENAME)
        or "attachment"
    )


def _attachment_bytes(att) -> bytes:
    size = getattr(att, "size", 0) or 0
    return bytes(att.read_buffer(size)) if size else b""


def _fmt(value) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else _text(value)


def _recipients(item) -> list[tuple[int, str]]:
    """(recipient type, 'Name <addr>') per recipient; type 1=to 2=cc 3=bcc."""
    table = getattr(item, "recipients", None)
    if table is None:
        return []
    out: list[tuple[int, str]] = []
    try:
        record_sets = list(table.record_sets or [])
    except Exception:
        return []
    for rs in record_sets:
        holder = _RecordSets(rs)
        name = _prop_str(holder, PR_DISPLAY_NAME)
        addr = _prop_str(holder, PR_SMTP_ADDRESS) or _prop_str(holder, PR_EMAIL_ADDRESS)
        rtype = _prop_int(holder, PR_RECIPIENT_TYPE) or 1
        if name and addr and name != addr:
            label = f"{name} <{addr}>"
        else:
            label = addr or name
        if label:
            out.append((rtype, label))
    return out


class _RecordSets:
    """Adapter so a single record set can go through the obj.record_sets helpers."""

    def __init__(self, rs):
        self.record_sets = [rs]


def _line(label: str, value: str) -> str | None:
    value = (value or "").strip()
    return f"{label}: {value}" if value else None


def _render_properties(item, kind: str) -> str:
    lines: list[str | None] = []
    if kind == "contact":
        lines += [
            _line("Name", _prop_str(item, PR_DISPLAY_NAME)),
            _line("Company", _prop_str(item, PR_COMPANY_NAME)),
            _line("Business phone", _prop_str(item, PR_BUSINESS_PHONE)),
            _line("Mobile phone", _prop_str(item, PR_MOBILE_PHONE)),
        ]
    else:
        names = [a for _, a in _recipients(item)]
        label = "Organizer" if kind == "calendar" else "From"
        lines.append(_line(label, _text(getattr(item, "sender_name", None))))
        lines.append(_line("Attendees" if kind == "calendar" else "To", "; ".join(names)))
        for lbl, attr in (
            ("Sent", "client_submit_time"),
            ("Delivered", "delivery_time"),
            ("Created", "creation_time"),
        ):
            lines.append(_line(lbl, _fmt(getattr(item, attr, None))))
    rendered = [ln for ln in lines if ln]
    body = _text(getattr(item, "plain_text_body", None)).strip()
    if body:
        rendered += ["", body]
    return "\n".join(rendered)


def to_parsed(item, kind: str) -> ParsedMessage:
    subject = _text(getattr(item, "subject", None))
    if kind != "email":
        return ParsedMessage(subject=subject, body_text=_render_properties(item, kind))

    h = _headers(_text(getattr(item, "transport_headers", None)))

    def hv(name: str) -> str:
        return decode_header_value(h.get(name, ""))

    recips = _recipients(item)

    def addrs(rtype: int) -> str:
        return ", ".join(a for t, a in recips if t == rtype)

    html = _text(getattr(item, "html_body", None))
    body_text = _text(getattr(item, "plain_text_body", None)).strip()
    if not body_text and html:
        body_text = _strip_html(html).strip()

    attachments: list[tuple[str, bytes]] = []
    inline: set[str] = set()
    for att in _attachments(item):
        name = _attachment_name(att)
        attachments.append((name, _attachment_bytes(att)))
        cid = _prop_str(att, PR_ATTACH_CONTENT_ID).strip("<>")
        if cid and html and re.search("cid:" + re.escape(cid), html, re.I):
            inline.add(name)

    date_sent = (
        hv("date")
        or _fmt(getattr(item, "client_submit_time", None))
        or _fmt(getattr(item, "delivery_time", None))
        or None
    )

    return ParsedMessage(
        from_=hv("from") or _text(getattr(item, "sender_name", None)).strip(),
        to=hv("to") or addrs(1),
        cc=hv("cc") or addrs(2),
        bcc=hv("bcc") or addrs(3),
        subject=subject or hv("subject"),
        date_sent=date_sent,
        body_text=body_text,
        body_html=html,
        attachments=attachments,
        message_id=hv("message-id"),
        in_reply_to=hv("in-reply-to"),
        references=" ".join(hv("references").split()),
        inline_names=inline,
    )
