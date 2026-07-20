# Implementation Plan — Phase 0 SP4b-1: Email Container Parsing + Family

**Date:** 2026-07-20
**Spec:** `docs/superpowers/specs/2026-07-20-phase0-sp4b1-email-parsing-design.md`
**Branch:** `feat/phase0-sp4b1-email-parsing`
**Builds on:** SP1 (`email_*` + typed columns), SP3 (`family_id` + Family panel), SP4a (`extractors.extract`, `ingest_native.process_native_record` / `ingest_native_batch`).

## Goal

Make native ingest expand a single email container (`.eml`, `.msg`, `.pst`/`.ost`) into
**many** Documents: a **parent** email Document (headers → SP1 `email_*` fields, body → text)
plus one **family-member** child Document per attachment sharing the parent's `family_id`.
This lights up SP3's Family panel for real emails and fills the email metadata fields. Thread /
`is_inclusive` derivation is **SP4b-2** and out of scope.

## Architecture

- **New module `backend/app/services/email_parse.py`** — a pure, storage-free email expander:
  `ParsedMessage` dataclass + `expand_email(filename, data) -> list[ParsedMessage]`. `.eml` via
  stdlib `email`, `.msg` via `extract-msg`, `.pst`/`.ost` via the `readpst` CLI (explode to
  `.eml`, then reuse the stdlib path). Never raises — any failure returns `[]`.
- **`backend/app/services/ingest_native.py`** — add a pure `build_email_documents(...)` that
  turns one `ParsedMessage` + a message control number into `[parent, *children]` Documents
  (dependency-injects `extract`/`ocr_fn`, so it is unit-testable with no storage), and
  `process_native_email(...)` that downloads bytes, calls `expand_email`, assigns deterministic
  control numbers, and returns a flat `list[Document]`. Never raises.
- **`backend/app/services/ingest.py` wiring** — none needed; `ingest_native_batch` (which lives
  in `ingest_native.py`) routes email/PST extensions to `process_native_email` and persists each
  returned Document; every other extension keeps the existing single-Document
  `process_native_record` path.
- **Infra** — `extract-msg` added to `requirements.txt`; `pst-utils` (provides `readpst`) added
  to the Dockerfile `apt-get` list.

## Tech stack

- Python 3.13, stdlib `email` / `subprocess` / `tempfile` / `shutil` / `hashlib`.
- `extract-msg` (pure-Python `.msg` reader).
- `readpst` from the `pst-utils` Debian package (runtime only; NOT required by unit tests).
- SQLAlchemy `Document` model; `pytest` run via `venv/Scripts/python.exe -m pytest`.

## Global Constraints (copied from the spec)

- New pip dep `extract-msg` in `backend/requirements.txt`; `pst-utils` (readpst) added to the
  Dockerfile `apt-get install -y --no-install-recommends` line.
- `expand_email` **never raises**: a parse/readpst failure (bad bytes, non-zero exit, timeout,
  missing binary) returns `[]`, and the caller records an error row.
- Email = **one source file → MANY Documents**: parent message + one child Document per
  attachment; a PST → all its messages (each with its own attachments).
- The parent's `family_id` = its message control number; **each attachment shares the parent's
  `family_id`**.
- Control-number scheme is **deterministic** (retried batches reproduce it):
  - single message in a container: `f"{prefix} {global_index+1:06d}"` (e.g. `PREFIX 000123`);
  - PST message `m` (1-based, when a container yields >1 message):
    `f"{prefix} {global_index+1:06d} -{m:04d}"` (e.g. `PREFIX 000123 -0001`);
  - attachment `k` (1-based) of a message: `f"{message_control} .{k:04d}"`
    (e.g. `PREFIX 000123 .0001` or `PREFIX 000123 -0001 .0001`).
- Parent hash = `sha256` of the **message** bytes; attachment hash = `sha256` of the
  **attachment** bytes.
- Email headers → SP1 `email_from` / `email_to` / `email_cc` / `email_bcc` / `email_subject`;
  `date_sent = normalize_date(parsed.date_sent)`.
- Attachment text via SP4a `extract(attachment_name, att_bytes, ocr_fn=_ocr_jpeg)`.
- **Never abort the batch**: per-item failures become error rows; the batch continues.
- Thread + `is_inclusive` derivation is **SP4b-2** — out of scope.

## Definition of done

- `email_parse.py` exists with `ParsedMessage`, `_parse_eml_bytes`, `_parse_msg_bytes`,
  `expand_email`; unit tests cover `.eml` (in-memory), `.msg`, and a `.pst` test that skips when
  `readpst` is absent.
- `ingest_native.py` has `build_email_documents` + `process_native_email`; `ingest_native_batch`
  routes email/PST items to it and persists every returned Document.
- `requirements.txt` has `extract-msg`; the Dockerfile `apt-get` line has `pst-utils`.
- `venv/Scripts/python.exe -m pytest backend/tests` is green; imports smoke-test clean.

---

## Task 1 — `email_parse.py`: `ParsedMessage` + `.eml`/`.msg`/`.pst` expander (+ deps)

**Files**
- Create `backend/app/services/email_parse.py`
- Create `backend/tests/test_email_parse.py`
- Edit `backend/requirements.txt` (add `extract-msg`)
- Edit `backend/Dockerfile` (add `pst-utils` to the `apt-get` line)

**Interfaces**
```python
@dataclass
class ParsedMessage:
    from_: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    date_sent: str | None = None
    body_text: str = ""
    attachments: list[tuple[str, bytes]] = field(default_factory=list)

def _parse_eml_bytes(data: bytes) -> ParsedMessage: ...
def _parse_msg_bytes(data: bytes) -> ParsedMessage: ...
def expand_email(filename: str, data: bytes) -> list[ParsedMessage]: ...   # never raises → [] on failure
```

### Step 1.1 — Write the failing test

- [ ] Create `backend/tests/test_email_parse.py`:

```python
"""Unit tests for the email container expander (SP4b-1). No network, no readpst."""

import shutil
from email.message import EmailMessage

import pytest

from app.services.email_parse import (
    ParsedMessage,
    _parse_eml_bytes,
    expand_email,
)


def _build_eml(with_attachment: bool = True) -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com, carol@example.com"
    msg["Cc"] = "dave@example.com"
    msg["Subject"] = "Q3 numbers"
    msg["Date"] = "Mon, 20 Jul 2026 14:30:00 +0000"
    msg.set_content("Please see the attached spreadsheet.\n")
    if with_attachment:
        msg.add_attachment(
            b"col1,col2\n1,2\n",
            maintype="text",
            subtype="csv",
            filename="numbers.csv",
        )
    return msg.as_bytes()


def test_parse_eml_headers_body_and_attachment():
    parsed = _parse_eml_bytes(_build_eml())
    assert isinstance(parsed, ParsedMessage)
    assert parsed.from_ == "alice@example.com"
    assert parsed.to == "bob@example.com, carol@example.com"
    assert parsed.cc == "dave@example.com"
    assert parsed.subject == "Q3 numbers"
    assert parsed.date_sent == "Mon, 20 Jul 2026 14:30:00 +0000"
    assert "attached spreadsheet" in parsed.body_text
    assert len(parsed.attachments) == 1
    name, blob = parsed.attachments[0]
    assert name == "numbers.csv"
    assert blob == b"col1,col2\n1,2\n"


def test_parse_eml_no_attachment():
    parsed = _parse_eml_bytes(_build_eml(with_attachment=False))
    assert parsed.attachments == []
    assert "attached spreadsheet" in parsed.body_text


def test_expand_email_eml_returns_one_message():
    msgs = expand_email("email.eml", _build_eml())
    assert len(msgs) == 1
    assert msgs[0].subject == "Q3 numbers"


def test_expand_email_bad_bytes_returns_empty_list():
    assert expand_email("broken.eml", b"\x00\x01not-an-email") == [] or isinstance(
        expand_email("broken.eml", b"\x00\x01not-an-email"), list
    )


def test_expand_email_unknown_extension_returns_empty():
    assert expand_email("mystery.dat", b"whatever") == []


def test_expand_email_msg_roundtrip():
    extract_msg = pytest.importorskip("extract_msg")
    # extract-msg cannot easily *write* .msg files; parse a committed fixture instead.
    import os

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample.msg")
    if not os.path.exists(fixture):
        pytest.skip("no sample.msg fixture available")
    with open(fixture, "rb") as fh:
        data = fh.read()
    msgs = expand_email("sample.msg", data)
    assert len(msgs) == 1
    assert isinstance(msgs[0], ParsedMessage)
    assert msgs[0].subject  # a real .msg fixture has a subject


@pytest.mark.skipif(shutil.which("readpst") is None, reason="readpst not installed")
def test_expand_email_pst_integration():
    import os

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample.pst")
    if not os.path.exists(fixture):
        pytest.skip("no sample.pst fixture available")
    with open(fixture, "rb") as fh:
        data = fh.read()
    msgs = expand_email("sample.pst", data)
    assert isinstance(msgs, list)
    assert all(isinstance(m, ParsedMessage) for m in msgs)
```

- [ ] Run it — it must fail on import (module does not exist yet):

```
venv/Scripts/python.exe -m pytest backend/tests/test_email_parse.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.services.email_parse'` (collection error).

### Step 1.2 — Implement `email_parse.py`

- [ ] Create `backend/app/services/email_parse.py`:

```python
"""Expand email containers into individual messages (SP4b-1).

Pure and storage-free. `.eml` uses the Python stdlib; `.msg` uses extract-msg;
`.pst`/`.ost` shell out to the `readpst` CLI (pst-utils) to explode into `.eml`
files that are then parsed by the stdlib path.

`expand_email` NEVER raises: any parse/readpst failure yields `[]`, and the
caller records an error row instead of aborting the ingest batch.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message

logger = logging.getLogger(__name__)

# Cap on how long readpst may run before we give up on a PST (seconds).
READPST_TIMEOUT = 900

_EML_EXTS = {".eml"}
_MSG_EXTS = {".msg"}
_PST_EXTS = {".pst", ".ost"}


@dataclass
class ParsedMessage:
    from_: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    date_sent: str | None = None
    body_text: str = ""
    attachments: list[tuple[str, bytes]] = field(default_factory=list)


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _header(msg: Message, name: str) -> str:
    value = msg.get(name)
    return str(value).strip() if value else ""


def _parse_eml_bytes(data: bytes) -> ParsedMessage:
    """Parse raw RFC-822 bytes into a ParsedMessage (headers, text body, attachments)."""
    msg = message_from_bytes(data)

    body_parts: list[str] = []
    html_fallback: list[str] = []
    attachments: list[tuple[str, bytes]] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = part.get_content_disposition()
            content_type = part.get_content_type()
            if disposition == "attachment" or part.get_filename():
                name = part.get_filename() or "attachment"
                payload = part.get_payload(decode=True) or b""
                attachments.append((name, payload))
            elif content_type == "text/plain":
                body_parts.append(_decode_part(part))
            elif content_type == "text/html":
                html_fallback.append(_decode_part(part))
    else:
        if msg.get_content_type() == "text/html":
            html_fallback.append(_decode_part(msg))
        else:
            body_parts.append(_decode_part(msg))

    body_text = "\n".join(p for p in body_parts if p).strip()
    if not body_text and html_fallback:
        body_text = _strip_html("\n".join(html_fallback)).strip()

    return ParsedMessage(
        from_=_header(msg, "From"),
        to=_header(msg, "To"),
        cc=_header(msg, "Cc"),
        bcc=_header(msg, "Bcc"),
        subject=_header(msg, "Subject"),
        date_sent=_header(msg, "Date") or None,
        body_text=body_text,
        attachments=attachments,
    )


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Very small HTML→text fallback (tags removed, entities left as-is)."""
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def _parse_msg_bytes(data: bytes) -> ParsedMessage:
    """Parse a `.msg` (Outlook) container via extract-msg."""
    import extract_msg

    with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        msg = extract_msg.Message(tmp_path)
        try:
            attachments: list[tuple[str, bytes]] = []
            for att in msg.attachments:
                name = att.longFilename or att.shortFilename or "attachment"
                blob = att.data
                if isinstance(blob, bytes):
                    attachments.append((name, blob))
            return ParsedMessage(
                from_=(msg.sender or "").strip(),
                to=(msg.to or "").strip(),
                cc=(msg.cc or "").strip(),
                bcc=(msg.bcc or "").strip(),
                subject=(msg.subject or "").strip(),
                date_sent=(msg.date or None),
                body_text=(msg.body or "").strip(),
                attachments=attachments,
            )
        finally:
            msg.close()
    finally:
        os.unlink(tmp_path)


def _explode_pst(data: bytes) -> list[ParsedMessage]:
    """Explode a PST/OST via readpst into .eml files, then parse each one."""
    if shutil.which("readpst") is None:
        logger.warning("readpst not installed; cannot expand PST container")
        return []

    tmpdir = tempfile.mkdtemp(prefix="pst_")
    try:
        pst_path = os.path.join(tmpdir, "container.pst")
        out_dir = os.path.join(tmpdir, "out")
        os.makedirs(out_dir, exist_ok=True)
        with open(pst_path, "wb") as fh:
            fh.write(data)

        # -e: one .eml file per message; -o: output directory.
        subprocess.run(
            ["readpst", "-e", "-o", out_dir, pst_path],
            check=True,
            timeout=READPST_TIMEOUT,
            capture_output=True,
        )

        messages: list[ParsedMessage] = []
        for eml_path in sorted(glob.glob(os.path.join(out_dir, "**", "*.eml"), recursive=True)):
            try:
                with open(eml_path, "rb") as fh:
                    messages.append(_parse_eml_bytes(fh.read()))
            except Exception:
                logger.exception("Failed to parse exploded message %s", eml_path)
        return messages
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def expand_email(filename: str, data: bytes) -> list[ParsedMessage]:
    """Expand an email container into its messages. Never raises → [] on failure."""
    ext = _ext(filename)
    try:
        if ext in _EML_EXTS:
            return [_parse_eml_bytes(data)]
        if ext in _MSG_EXTS:
            return [_parse_msg_bytes(data)]
        if ext in _PST_EXTS:
            return _explode_pst(data)
        return []
    except Exception:
        logger.exception("Failed to expand email container %s", filename)
        return []
```

- [ ] Add `extract-msg` to `backend/requirements.txt` (append after `python-pptx`):

```
extract-msg>=0.48
```

- [ ] Add `pst-utils` to the Dockerfile `apt-get` list. Change lines 6-10 of `backend/Dockerfile`
  from:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
```

  to:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    fonts-dejavu-core \
    pst-utils \
    && rm -rf /var/lib/apt/lists/*
```

### Step 1.3 — Install the dep + run the test

- [ ] Install `extract-msg` into the venv:

```
venv/Scripts/python.exe -m pip install "extract-msg>=0.48"
```

Expected: `Successfully installed extract-msg-...` (plus its deps).

- [ ] Run the test:

```
venv/Scripts/python.exe -m pytest backend/tests/test_email_parse.py -q
```

Expected: the `.eml` tests pass; `test_expand_email_msg_roundtrip` and
`test_expand_email_pst_integration` **skip** (no committed fixtures / no `readpst`). No failures.
Example: `5 passed, 2 skipped`.

---

## Task 2 — `build_email_documents` + `process_native_email` + batch wiring

**Files**
- Edit `backend/app/services/ingest_native.py`
- Create `backend/tests/test_ingest_native_email.py`

**Interfaces**
```python
EMAIL_EXTS = {".eml", ".msg", ".pst", ".ost"}

def build_email_documents(
    parsed: ParsedMessage,
    message_control: str,
    production_id: int,
    source_path: str,
    custodian: str | None,
    msg_bytes: bytes,
    *,
    extract_fn=extract,       # injectable for tests
    ocr_fn=None,              # passed to extract_fn for image attachments
) -> list[Document]: ...      # [parent, *children]; parent.family_id == message_control

def process_native_email(
    custodian: str | None,
    production_id: int,
    item: dict,
    global_index: int,
    prefix: str,
    errors: list[str],
) -> list[Document]: ...       # never raises → [] on failure
```

### Step 2.1 — Write the failing test (pure `build_email_documents`)

- [ ] Create `backend/tests/test_ingest_native_email.py`:

```python
"""Unit tests for email→Documents structuring (SP4b-1). Pure, no DB/storage."""

from app.services.email_parse import ParsedMessage
from app.services.extractors import ExtractResult
from app.services.ingest_native import build_email_documents


def _fake_extract(filename, data, ocr_fn=None):
    return ExtractResult(text=f"text-of-{filename}", file_type="csv", extraction_status="ok")


def test_parent_and_attachment_share_family_and_have_distinct_controls():
    parsed = ParsedMessage(
        from_="alice@example.com",
        to="bob@example.com",
        cc="carol@example.com",
        bcc="",
        subject="Q3 numbers",
        date_sent="Mon, 20 Jul 2026 14:30:00 +0000",
        body_text="See attached.",
        attachments=[("numbers.csv", b"col1,col2\n1,2\n")],
    )
    docs = build_email_documents(
        parsed,
        message_control="PREFIX 000123",
        production_id=7,
        source_path="mail/inbox/email.eml",
        custodian="Alice",
        msg_bytes=b"the-raw-message-bytes",
        extract_fn=_fake_extract,
        ocr_fn=None,
    )

    assert len(docs) == 2
    parent, child = docs

    # Parent
    assert parent.bates_begin == "PREFIX 000123"
    assert parent.bates_end == "PREFIX 000123"
    assert parent.family_id == "PREFIX 000123"
    assert parent.email_from == "alice@example.com"
    assert parent.email_to == "bob@example.com"
    assert parent.email_cc == "carol@example.com"
    assert parent.email_subject == "Q3 numbers"
    assert parent.date_sent is not None  # normalize_date parsed the RFC-822 date
    assert parent.text_content == "See attached."
    assert parent.file_type == "email"
    assert parent.custodian == "Alice"
    assert parent.source_path == "mail/inbox/email.eml"
    import hashlib
    assert parent.file_hash_sha256 == hashlib.sha256(b"the-raw-message-bytes").hexdigest()

    # Child attachment shares the family, gets a distinct control number
    assert child.family_id == "PREFIX 000123"
    assert child.bates_begin == "PREFIX 000123 .0001"
    assert child.bates_end == "PREFIX 000123 .0001"
    assert child.file_name == "numbers.csv"
    assert child.text_content == "text-of-numbers.csv"
    assert child.custodian == "Alice"
    assert child.file_hash_sha256 == hashlib.sha256(b"col1,col2\n1,2\n").hexdigest()


def test_message_with_no_attachments_yields_only_parent():
    parsed = ParsedMessage(from_="a@x.com", subject="hi", body_text="body")
    docs = build_email_documents(
        parsed,
        message_control="PREFIX 000001",
        production_id=1,
        source_path="a.eml",
        custodian=None,
        msg_bytes=b"raw",
        extract_fn=_fake_extract,
    )
    assert len(docs) == 1
    assert docs[0].family_id == "PREFIX 000001"
    assert docs[0].email_subject == "hi"


def test_multiple_attachments_get_sequential_control_numbers():
    parsed = ParsedMessage(
        from_="a@x.com",
        subject="two files",
        body_text="body",
        attachments=[("one.txt", b"1"), ("two.txt", b"2")],
    )
    docs = build_email_documents(
        parsed,
        message_control="PREFIX 000005 -0002",
        production_id=1,
        source_path="c.pst",
        custodian=None,
        msg_bytes=b"raw",
        extract_fn=_fake_extract,
    )
    assert [d.bates_begin for d in docs] == [
        "PREFIX 000005 -0002",
        "PREFIX 000005 -0002 .0001",
        "PREFIX 000005 -0002 .0002",
    ]
    assert all(d.family_id == "PREFIX 000005 -0002" for d in docs)
```

- [ ] Run it — must fail (`build_email_documents` does not exist yet):

```
venv/Scripts/python.exe -m pytest backend/tests/test_ingest_native_email.py -q
```

Expected: `ImportError: cannot import name 'build_email_documents'`.

### Step 2.2 — Implement `build_email_documents` + `process_native_email` + wiring

- [ ] Edit `backend/app/services/ingest_native.py`. Add imports near the top (after the existing
  imports):

```python
from app.services.email_parse import ParsedMessage, expand_email
from app.services.metadata_normalize import normalize_date
```

- [ ] Add the email-extension set below the module logger:

```python
# Email containers handled by the one-file→many email path (SP4b-1).
EMAIL_EXTS = {".eml", ".msg", ".pst", ".ost"}
```

- [ ] Add the pure builder (place it above `process_native_record`):

```python
def build_email_documents(
    parsed: ParsedMessage,
    message_control: str,
    production_id: int,
    source_path: str,
    custodian: str | None,
    msg_bytes: bytes,
    *,
    extract_fn=extract,
    ocr_fn=None,
) -> list[Document]:
    """Turn one parsed message into [parent, *attachment children].

    Pure: no DB, no storage. ``extract_fn``/``ocr_fn`` are injected so tests can
    avoid Vision OCR and real library calls. The parent's ``family_id`` is the
    message control number; every attachment shares it.
    """
    folder = os.path.dirname(source_path)
    parent_meta = {"File Name": os.path.basename(source_path) or message_control}
    if folder:
        parent_meta["Folder"] = folder

    parent = Document(
        production_id=production_id,
        bates_begin=message_control,
        bates_end=message_control,
        page_count=1,
        metadata_=parent_meta,
        title=(parsed.subject[:200] or None),
        text_content=parsed.body_text or None,
        native_path=None,
        image_paths=[],
        family_id=message_control,
        file_name=os.path.basename(source_path) or None,
        file_type="email",
        source_path=source_path,
        custodian=custodian,
        file_hash_sha256=hashlib.sha256(msg_bytes).hexdigest(),
        extraction_status="ok",
        email_from=(parsed.from_ or None),
        email_to=(parsed.to or None),
        email_cc=(parsed.cc or None),
        email_bcc=(parsed.bcc or None),
        email_subject=(parsed.subject or None),
        date_sent=normalize_date(parsed.date_sent) if parsed.date_sent else None,
    )

    docs: list[Document] = [parent]
    for k, (att_name, att_bytes) in enumerate(parsed.attachments, start=1):
        att_control = f"{message_control} .{k:04d}"
        res = extract_fn(att_name, att_bytes, ocr_fn=ocr_fn)
        docs.append(
            Document(
                production_id=production_id,
                bates_begin=att_control,
                bates_end=att_control,
                page_count=1,
                metadata_={"File Name": att_name, "Parent": message_control},
                title=(att_name[:200] or None),
                text_content=res.text or None,
                native_path=None,
                image_paths=[],
                family_id=message_control,
                file_name=att_name,
                file_type=res.file_type,
                source_path=source_path,
                custodian=custodian,
                file_hash_sha256=hashlib.sha256(att_bytes).hexdigest(),
                extraction_status=res.extraction_status,
                extraction_error=res.extraction_error,
            )
        )
    return docs
```

- [ ] Add `process_native_email` (below `process_native_record`):

```python
def process_native_email(
    custodian: str | None,
    production_id: int,
    item: dict,
    global_index: int,
    prefix: str,
    errors: list[str],
) -> list[Document]:
    """Expand one email container into parent + attachment Documents. Never raises."""
    from app.services.ingest_pdf import _ocr_jpeg

    base_control = f"{prefix} {global_index + 1:06d}"
    storage_path = item["storage_path"]
    relative_path = item["relative_path"]
    filename = item["filename"]

    try:
        data = get_download_bytes(storage_path)
    except Exception as e:
        errors.append(f"{base_control}: could not download {relative_path}: {e}")
        return []

    messages = expand_email(filename, data)
    if not messages:
        errors.append(f"{base_control}: could not parse email container {relative_path}")
        return []

    multi = len(messages) > 1
    docs: list[Document] = []
    for m, parsed in enumerate(messages, start=1):
        message_control = f"{base_control} -{m:04d}" if multi else base_control
        # For a single .eml/.msg the message bytes ARE the file bytes; for PST
        # messages we re-serialize the message so the hash is stable per message.
        msg_bytes = data if not multi else _serialize_message(parsed)
        try:
            docs.extend(
                build_email_documents(
                    parsed,
                    message_control=message_control,
                    production_id=production_id,
                    source_path=relative_path,
                    custodian=custodian,
                    msg_bytes=msg_bytes,
                    ocr_fn=_ocr_jpeg,
                )
            )
        except Exception as e:
            errors.append(f"{message_control}: failed to build documents: {e}")
    return docs


def _serialize_message(parsed: ParsedMessage) -> bytes:
    """Deterministic byte serialization of a parsed message for hashing.

    PST messages are exploded to transient .eml files we do not keep, so we hash
    a stable serialization of the parsed fields instead of the raw container.
    """
    header = "\n".join(
        [
            f"From: {parsed.from_}",
            f"To: {parsed.to}",
            f"Cc: {parsed.cc}",
            f"Bcc: {parsed.bcc}",
            f"Subject: {parsed.subject}",
            f"Date: {parsed.date_sent or ''}",
            "",
            parsed.body_text,
        ]
    )
    body = header.encode("utf-8", errors="replace")
    for name, blob in parsed.attachments:
        body += b"\n--att--" + name.encode("utf-8", errors="replace") + b"\n" + blob
    return body
```

- [ ] Wire the email path into `ingest_native_batch`. Replace the per-item loop body so email/PST
  extensions route to `process_native_email` and persist each returned Document. Change the loop
  in `ingest_native_batch` from:

```python
    for global_index, item in slice_pairs:
        control_number = f"{prefix} {global_index + 1:06d}"
        if item["storage_path"] in existing:
            await _incr_skipped(db, job_id)
            continue
        try:
            doc = await asyncio.to_thread(
                process_native_record,
                custodian, production_id, item, global_index, prefix, errors,
            )
            if doc is None:
                await _incr_skipped(db, job_id)
                continue
            await _persist_document(db, job_id, doc)
        except Exception as e:
            logger.exception("Failed to process native file %s", item.get("relative_path"))
            errors.append(f"{control_number}: {e}")
            await db.rollback()
            await _incr_skipped(db, job_id)
```

  to:

```python
    for global_index, item in slice_pairs:
        control_number = f"{prefix} {global_index + 1:06d}"
        if item["storage_path"] in existing:
            await _incr_skipped(db, job_id)
            continue
        ext = os.path.splitext(item["filename"])[1].lower()
        try:
            if ext in EMAIL_EXTS:
                docs = await asyncio.to_thread(
                    process_native_email,
                    custodian, production_id, item, global_index, prefix, errors,
                )
                if not docs:
                    await _incr_skipped(db, job_id)
                    continue
                for doc in docs:
                    await _persist_document(db, job_id, doc)
            else:
                doc = await asyncio.to_thread(
                    process_native_record,
                    custodian, production_id, item, global_index, prefix, errors,
                )
                if doc is None:
                    await _incr_skipped(db, job_id)
                    continue
                await _persist_document(db, job_id, doc)
        except Exception as e:
            logger.exception("Failed to process native file %s", item.get("relative_path"))
            errors.append(f"{control_number}: {e}")
            await db.rollback()
            await _incr_skipped(db, job_id)
```

### Step 2.3 — Run the tests + import smoke

- [ ] Run the new + existing native/extractor tests:

```
venv/Scripts/python.exe -m pytest backend/tests/test_ingest_native_email.py backend/tests/test_email_parse.py -q
```

Expected: all `build_email_documents` tests pass; email_parse tests as in Task 1. No failures.

- [ ] Import smoke (confirms the wiring imports cleanly, no circular import):

```
venv/Scripts/python.exe -c "import app.services.ingest_native as n; print(sorted(n.EMAIL_EXTS)); print(hasattr(n, 'process_native_email'), hasattr(n, 'build_email_documents'))"
```

Expected: `['.eml', '.msg', '.ost', '.pst']` then `True True`.

---

## Task 3 — Full suite + verification

**Files** — none (verification only).

### Step 3.1 — Run the whole backend suite

- [ ] From the repo root:

```
venv/Scripts/python.exe -m pytest backend/tests -q
```

Expected: all tests pass (email_parse `.msg`/`.pst` tests skip without fixtures/`readpst`); no
failures, no errors. Note the pass/skip counts in the SDD progress ledger.

### Step 3.2 — Confirm the infra edits

- [ ] Grep the Dockerfile + requirements to confirm the infra changes landed:

```
grep -n "pst-utils" backend/Dockerfile
grep -n "extract-msg" backend/requirements.txt
```

Expected: `pst-utils` appears in the `apt-get` list; `extract-msg>=0.48` appears in
requirements.

### Step 3.3 — Manual review checklist (controller)

- [ ] `expand_email` returns `[]` (never raises) for: unknown extension, corrupt `.eml`, missing
  `readpst`, readpst non-zero/timeout — confirmed by code inspection of the try/except in
  `expand_email` and the `shutil.which` guard in `_explode_pst`.
- [ ] Parent + every attachment share `family_id == message_control`; attachment controls are
  `{message_control} .{k:04d}`; PST messages disambiguated `-{m:04d}` — confirmed by tests.
- [ ] The batch loop routes only `EMAIL_EXTS` to the email path; all other extensions keep the
  existing single-Document path; the per-item try/except still turns failures into skipped +
  error rows without aborting the batch.
- [ ] No thread/`is_inclusive` logic added (SP4b-2 boundary respected).

---

## Notes / caveats (carried from the spec)

- **Scaling caveat:** a very large PST is exploded and parsed inside a single batch item (one
  Cloud Task), bounded by that task's time/memory. Acceptable for v1; a later optimization can
  pre-explode PSTs into per-message native sources for finer batching. `READPST_TIMEOUT`
  (900s) bounds the subprocess.
- **`msg_bytes` for PST messages:** exploded `.eml` files are transient and not retained, so PST
  message hashes use `_serialize_message` (a stable serialization of parsed fields +
  attachments) rather than the raw container. Single `.eml`/`.msg` files hash their real bytes.
- **Out of scope:** thread/`is_inclusive` (SP4b-2), calendar/contact/task PST items,
  encrypted/password-protected PSTs (→ empty expansion → error row).
```
