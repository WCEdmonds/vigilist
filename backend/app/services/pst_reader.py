"""Read a large PST/OST with pypff from a local copy.

Streaming pypff reads from Cloud Storage was measured unusable (8-10x read
amplification), so only the ``vigilist-pst-explode`` Cloud Run Job opens a PST:
it downloads it to in-memory /tmp, surveys it and explodes every item to
Storage (``pst_explode``). Chunk workers then read the exploded objects
(``exploded_reader``), never the PST.

The survey walks the folder tree once (counts + bytes by item type);
``iter_slice`` yields exactly the ``[folder, start, end)`` ranges a chunk owns.
``to_parsed`` reads one item field by field: a field that fails is recorded in
``ParsedMessage.read_errors`` and the rest of the item is kept; an attachment
that cannot be read becomes an ``UnreadableAttachment`` (an error document).

pypff is imported lazily inside ``open_pst`` so everything else here is
testable with a fake item tree.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Iterator

from app.services.email_parse import (
    OversizedAttachment,
    ParsedMessage,
    RawEmbeddedMessage,
    UnreadableAttachment,
    _strip_html,
    decode_header_value,
    references_cid,
)

READER_MODE = "local_file"

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
    # Item types by position, run-length encoded: [[type, n], ...] in folder
    # order. Lets a chunk plan exact raw ranges in a folder that mixes
    # selected and unselected types.
    runs: list[list] = field(default_factory=list)


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
                    "runs": f.runs,
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
def open_pst(local_path: str, storage_path: str = ""):
    """Open a PST already copied to ``local_path`` (the explode Job's /tmp).

    ``storage_path`` is the container's object path, carried on the handle
    for the survey."""
    import pypff  # lazy: only the worker image needs it

    pst = pypff.file()
    pst.open(local_path)
    try:
        yield PstHandle(pst.get_root_folder(), storage_path)
    finally:
        pst.close()


def _root(handle):
    return handle.get_root_folder() if hasattr(handle, "get_root_folder") else handle


def _walk(folder, path: str, out: list[tuple[str, object]]) -> None:
    out.append((path, folder))
    for i in range(folder.get_number_of_sub_folders()):
        child = folder.get_sub_folder(i)
        name = getattr(child, "name", None) or f"folder{i}"
        _walk(child, f"{path}/{name}" if path else name, out)


def folders(handle) -> list[tuple[str, object]]:
    """``(path, folder)`` in traversal order; the list index is the folder index."""
    root = _root(handle)
    out: list[tuple[str, object]] = []
    _walk(root, getattr(root, "name", None) or "", out)
    return out


# MAPI property tags (non-named, so readable straight from record sets).
PR_MESSAGE_CLASS = 0x001A
PR_SUBJECT = 0x0037
PR_BODY = 0x1000
PR_CREATION_TIME = 0x3007
PR_LAST_MODIFICATION_TIME = 0x3008
PR_MESSAGE_SIZE = 0x0E08
PR_RECIPIENT_TYPE = 0x0C15
PR_DISPLAY_NAME = 0x3001
PR_EMAIL_ADDRESS = 0x3003
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_METHOD = 0x3705
PR_ATTACH_CONTENT_ID = 0x3712
PR_COMPANY_NAME = 0x3A16
PR_BUSINESS_PHONE = 0x3A08
PR_MOBILE_PHONE = 0x3A1C
PR_SMTP_ADDRESS = 0x39FE

# PR_ATTACH_METHOD values.
ATTACH_EMBEDDED_MSG = 5
ATTACH_OLE = 6


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


def item_kind(item) -> str:
    """Item type of one pypff item. The survey and every chunk reader use this
    same helper, so planned positions and read-time filtering agree."""
    return item_type(_prop_str(item, PR_MESSAGE_CLASS) or None)


class SurveyBuilder:
    """Accumulates a container survey folder by folder, in traversal order.
    ``survey`` and the explode walk share it, so both count positions the
    same way."""

    def __init__(self, storage_path: str):
        self.result = ContainerSurvey(storage_path=storage_path)

    def folder(self, idx: int, path: str) -> FolderSurvey:
        f = FolderSurvey(idx, path, is_recoverable(path),
                         {t: 0 for t in ITEM_TYPES}, {t: 0 for t in ITEM_TYPES}, [])
        self.result.folders.append(f)
        return f

    @staticmethod
    def add(f: FolderSurvey, kind: str, size: int) -> None:
        f.counts[kind] += 1
        f.bytes[kind] += size
        if f.runs and f.runs[-1][0] == kind:
            f.runs[-1][1] += 1
        else:
            f.runs.append([kind, 1])


def survey(handle) -> ContainerSurvey:
    builder = SurveyBuilder(getattr(handle, "storage_path", ""))
    for idx, (path, folder) in enumerate(folders(handle)):
        f = builder.folder(idx, path)
        for i in range(folder.get_number_of_sub_messages()):
            item = folder.get_sub_message(i)
            builder.add(f, item_kind(item), _item_size(item))
    return builder.result


def iter_slice(handle, slices: list[list[int]]) -> Iterator[tuple[int, FolderSurvey, int, object]]:
    all_folders = folders(handle)
    seq = 0
    for folder_index, start, end in slices:
        path, folder = all_folders[folder_index]
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


def short_error(e: BaseException) -> str:
    return " ".join(f"{type(e).__name__}: {e}".split())[:300]


# Bodies a message may simply not have: pypff raises "missing value data"
# for an absent HTML/RTF body (86 such items in the measured PST). That is
# not a read failure.
_OPTIONAL_FIELDS = ("html_body", "rtf_body")


class _Reader:
    """Reads one pypff object's attributes, recording each failure."""

    def __init__(self, errors: list[str]):
        self.errors = errors

    def get(self, obj, attr: str):
        try:
            return getattr(obj, attr, None)
        except Exception as e:
            if attr in _OPTIONAL_FIELDS and "missing value data" in str(e).lower():
                return None
            self.errors.append(f"{attr}: {short_error(e)}")
            return None

    def text(self, obj, attr: str) -> str:
        return _text(self.get(obj, attr))


def is_message(item) -> bool:
    """False for a bare ``pypff.item``: no message accessors at all (4.3% of
    the measured PST, all in the "other" class)."""
    try:
        return hasattr(item, "number_of_attachments")
    except Exception:
        return True


def _attachment_name(att) -> str:
    return (
        _text(getattr(att, "long_filename", None)).strip()
        or _prop_str(att, PR_ATTACH_FILENAME)
        or "attachment"
    )


# Attachments above this are not read into memory; they become an error
# document instead.
MAX_ATTACHMENT_BYTES = 512 * 1024 * 1024
# Embedded messages inside embedded messages are unpacked this deep.
MAX_EMBED_DEPTH = 3


def _attachment_bytes(att) -> bytes:
    size = getattr(att, "size", 0) or 0
    if size > MAX_ATTACHMENT_BYTES:
        return OversizedAttachment(size, MAX_ATTACHMENT_BYTES)
    return bytes(att.read_buffer(size)) if size else b""


def _embedded_item(att):
    """The message embedded in a method-5 attachment, or None.

    Tries an explicit embedded-item getter, then the attachment's first sub
    item (how pypff exposes an attachment's item tree)."""
    for getter in ("get_embedded_item", "get_item"):
        fn = getattr(att, getter, None)
        if callable(fn):
            try:
                item = fn()
            except Exception:
                item = None
            if item is not None:
                return item
    try:
        n = int(getattr(att, "number_of_sub_items", 0) or 0)
    except Exception:
        n = 0
    if n > 0:
        try:
            return att.get_sub_item(0)
        except Exception:
            return None
    return None


def to_eml_bytes(parsed: ParsedMessage) -> bytes:
    """RFC-822 rendering of a parsed message (an embedded-message attachment)."""
    from email.message import EmailMessage

    msg = EmailMessage()
    for name, value in (
        ("From", parsed.from_), ("To", parsed.to), ("Cc", parsed.cc), ("Bcc", parsed.bcc),
        ("Subject", parsed.subject), ("Date", parsed.date_sent or ""),
        ("Message-ID", parsed.message_id), ("In-Reply-To", parsed.in_reply_to),
        ("References", parsed.references),
    ):
        value = " ".join((value or "").split())
        if value:
            msg[name] = value
    msg.set_content(parsed.body_text or "")
    if parsed.body_html:
        msg.add_alternative(parsed.body_html, subtype="html")
    for name, data in parsed.attachments:
        if isinstance(data, (UnreadableAttachment, OversizedAttachment)):
            continue  # recorded in the outer message's read errors
        msg.add_attachment(bytes(data), maintype="application", subtype="octet-stream",
                           filename=name)
    return msg.as_bytes()


def _read_attachment(att, index: int, errors: list[str], depth: int) -> tuple[str, bytes]:
    """``(name, bytes)`` for one attachment. A failure becomes an
    ``UnreadableAttachment`` plus a read error, never a drop.

    Method 5 (embedded message) is unpacked as a nested message and rendered
    as an ``.eml`` attachment, so it becomes one attachment document of type
    "email" with its own text (headers, body, attachment names). One that
    can't be parsed keeps its raw bytes as ``<name>.msg-raw`` (a 'partial'
    document). Method 6 (OLE) is read as data; if that fails it is an error
    attachment."""
    try:
        name = _attachment_name(att)
    except Exception:
        name = f"attachment {index}"
    label = f"attachment {index} ({name})"
    method = _prop_int(att, PR_ATTACH_METHOD)

    if method == ATTACH_EMBEDDED_MSG:
        embedded = _embedded_item(att) if depth < MAX_EMBED_DEPTH else None
        why = "embedded message could not be opened as a message"
        if embedded is not None:
            try:
                inner = to_parsed(embedded, "email", _depth=depth + 1)
                for err in inner.read_errors:
                    errors.append(f"{label} embedded message: {err}")
                base = (inner.subject or "").strip() or name or "embedded message"
                return f"{base[:150]}.eml", to_eml_bytes(inner)
            except Exception as e:
                why = f"embedded message could not be read ({short_error(e)})"
        base = (_text(getattr(att, "long_filename", None)).strip()
                or _prop_str(att, PR_DISPLAY_NAME) or "attached message")
        raw_name = f"{base[:150]}.msg-raw"
        try:
            data = _attachment_bytes(att)
        except Exception as e:
            msg = f"{why}; raw data unreadable: {short_error(e)}"
            errors.append(f"attachment {index} ({raw_name}): {msg}")
            return raw_name, UnreadableAttachment(msg)
        if isinstance(data, OversizedAttachment):
            errors.append(f"attachment {index} ({raw_name}): {why}; too large to keep")
            return raw_name, data
        note = f"{why}; kept as raw data"
        errors.append(f"attachment {index} ({raw_name}): {note}")
        return raw_name, RawEmbeddedMessage(data, note)

    try:
        return name, _attachment_bytes(att)
    except Exception as e:
        kind = "OLE attachment" if method == ATTACH_OLE else "attachment"
        msg = f"{kind} could not be read: {short_error(e)}"
        errors.append(f"{label}: {msg}")
        return name, UnreadableAttachment(msg)


def _fmt(value) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else _text(value)


def _recipients(item, errors: list[str]) -> list[tuple[int, str]]:
    """(recipient type, 'Name <addr>') per recipient; type 1=to 2=cc 3=bcc."""
    table = _Reader(errors).get(item, "recipients")
    if table is None:
        return []  # no recipient table (2,022 items in the measured PST)
    out: list[tuple[int, str]] = []
    record_sets: list = []
    try:
        n = getattr(table, "number_of_recipients", None)
    except Exception:
        n = None
    if n is not None and callable(getattr(table, "get_recipient", None)):
        # pypff.recipients: one record set per recipient.
        for i in range(int(n)):
            try:
                record_sets.append(table.get_recipient(i))
            except Exception as e:
                errors.append(f"recipient {i + 1}: {short_error(e)}")
    else:
        try:
            record_sets = list(table.record_sets or [])
        except Exception as e:
            errors.append(f"recipients: {short_error(e)}")
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


def _render_properties(item, kind: str, r: _Reader) -> str:
    lines: list[str | None] = []
    if kind == "contact":
        lines += [
            _line("Name", _prop_str(item, PR_DISPLAY_NAME)),
            _line("Company", _prop_str(item, PR_COMPANY_NAME)),
            _line("Business phone", _prop_str(item, PR_BUSINESS_PHONE)),
            _line("Mobile phone", _prop_str(item, PR_MOBILE_PHONE)),
        ]
    else:
        names = [a for _, a in _recipients(item, r.errors)]
        label = "Organizer" if kind == "calendar" else "From"
        lines.append(_line(label, r.text(item, "sender_name")))
        lines.append(_line("Attendees" if kind == "calendar" else "To", "; ".join(names)))
        for lbl, attr in (
            ("Sent", "client_submit_time"),
            ("Delivered", "delivery_time"),
            ("Created", "creation_time"),
        ):
            lines.append(_line(lbl, _fmt(r.get(item, attr))))
    rendered = [ln for ln in lines if ln]
    body = r.text(item, "plain_text_body").strip()
    if body:
        rendered += ["", body]
    return "\n".join(rendered)


# String properties of a bare item rendered into its text, at most this many.
_BARE_MAX_PROPS = 200


def _prop_time(obj, tag: int) -> str:
    entry = _prop(obj, tag)
    if entry is None:
        return ""
    try:
        return _fmt(entry.data_as_datetime)
    except Exception:
        return ""


_BARE_RENDERED = (PR_MESSAGE_CLASS, PR_SUBJECT, PR_BODY, PR_CREATION_TIME,
                  PR_LAST_MODIFICATION_TIME)


def _render_bare(item) -> ParsedMessage:
    """A bare ``pypff.item`` (not a message; 2,185 in the measured PST): class,
    subject, times and body from its record sets (each optional), rendered
    like the other non-email kinds, then any other string properties. Never
    a read failure."""
    lines = [
        _line("Message class", _prop_str(item, PR_MESSAGE_CLASS)),
        _line("Created", _prop_time(item, PR_CREATION_TIME)),
        _line("Modified", _prop_time(item, PR_LAST_MODIFICATION_TIME)),
    ]
    lines = [ln for ln in lines if ln]
    extra = []
    seen = 0
    try:
        record_sets = list(item.record_sets or [])
    except Exception:
        record_sets = []
    for rs in record_sets:
        try:
            entries = list(rs.entries or [])
        except Exception:
            entries = []
        for entry in entries:
            if seen >= _BARE_MAX_PROPS:
                break
            try:
                tag = entry.entry_type
                if tag in _BARE_RENDERED:
                    continue
                value = entry.data_as_string
            except Exception:
                continue
            value = _text(value).strip() if isinstance(value, (str, bytes)) else ""
            if value:
                label = f"0x{int(tag):04X}" if isinstance(tag, int) else str(tag)
                extra.append(f"{label}: {value}")
                seen += 1
    lines += extra
    body = _prop_str(item, PR_BODY)
    if body:
        lines += ["", body]
    return ParsedMessage(subject=_prop_str(item, PR_SUBJECT), body_text="\n".join(lines))


def to_parsed(item, kind: str, _depth: int = 0) -> ParsedMessage:
    if not is_message(item):
        return _render_bare(item)
    errors: list[str] = []
    r = _Reader(errors)
    subject = r.text(item, "subject")
    if kind != "email":
        return ParsedMessage(subject=subject, body_text=_render_properties(item, kind, r),
                             read_errors=errors)

    h = _headers(r.text(item, "transport_headers"))

    def hv(name: str) -> str:
        return decode_header_value(h.get(name, ""))

    recips = _recipients(item, errors)

    def addrs(rtype: int) -> str:
        return ", ".join(a for t, a in recips if t == rtype)

    html = r.text(item, "html_body")
    body_text = r.text(item, "plain_text_body").strip()
    if not body_text and html:
        body_text = _strip_html(html).strip()

    attachments: list[tuple[str, bytes]] = []
    inline: set[str] = set()
    try:
        n = int(getattr(item, "number_of_attachments", 0) or 0)
    except Exception as e:
        errors.append(f"attachments: {short_error(e)}")
        n = 0
    for i in range(n):
        try:
            att = item.get_attachment(i)
        except Exception as e:
            msg = f"attachment could not be opened: {short_error(e)}"
            errors.append(f"attachment {i + 1}: {msg}")
            attachments.append((f"attachment {i + 1}", UnreadableAttachment(msg)))
            continue
        name, data = _read_attachment(att, i + 1, errors, _depth)
        attachments.append((name, data))
        cid = _prop_str(att, PR_ATTACH_CONTENT_ID).strip("<>")
        if cid and html and references_cid(html, cid):
            inline.add(name)

    date_sent = (
        hv("date")
        or _fmt(r.get(item, "client_submit_time"))
        or _fmt(r.get(item, "delivery_time"))
        or None
    )

    return ParsedMessage(
        from_=hv("from") or r.text(item, "sender_name").strip(),
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
        read_errors=errors,
    )
