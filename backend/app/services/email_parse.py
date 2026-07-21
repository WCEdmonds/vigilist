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
        # A single-part message can itself be an attachment (an inline image, a
        # forwarded document, an application/octet-stream body). Route those
        # through the attachment path so binary content isn't decoded as text.
        if msg.get_content_disposition() == "attachment" or msg.get_filename():
            name = msg.get_filename() or "attachment"
            payload = msg.get_payload(decode=True) or b""
            attachments.append((name, payload))
        elif msg.get_content_type() == "text/html":
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
                date_sent=(str(msg.date) if msg.date else None),
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

        # -e: one .eml file per message; -o: output directory. readpst's own
        # stdout/stderr is unused (messages are read from the .eml files it
        # writes), so discard it rather than buffering it in memory — a large
        # PST can otherwise emit substantial logging.
        subprocess.run(
            ["readpst", "-e", "-o", out_dir, pst_path],
            check=True,
            timeout=READPST_TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
