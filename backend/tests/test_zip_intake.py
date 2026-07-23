"""Unit tests for zip-container intake (Task F3). Pure, no DB/storage — real
in-memory zipfile fixtures, not mocks of the zipfile library."""

import io
import zipfile

import fitz  # PyMuPDF

from app.services import ingest_native as ingest_native_mod
from app.services import storage as storage_mod
from app.services.ingest_native import build_zip_documents


def _patch_central_dir_flag_bit(zip_bytes: bytes, name: str, bit: int = 0x1) -> bytes:
    """Flip a bit in the CENTRAL DIRECTORY flag_bits field for ``name``.

    ``zipfile.ZipFile.writestr`` always recomputes/clears flag_bits (stdlib
    has no write support for real ZipCrypto encryption), so the only way to
    build a fixture the encrypted-entry guard sees as encrypted — without
    mocking the zipfile library itself — is to write a normal zip then patch
    the on-disk central-directory record it produced. ``infolist()`` reads
    flag_bits from the central directory, which is all the guard inspects.
    """
    data = bytearray(zip_bytes)
    name_b = name.encode("utf-8")
    sig = b"PK\x01\x02"
    idx = 0
    while True:
        idx = data.find(sig, idx)
        if idx == -1:
            raise AssertionError(f"central directory entry for {name!r} not found")
        name_len = int.from_bytes(data[idx + 28:idx + 30], "little")
        extra_len = int.from_bytes(data[idx + 30:idx + 32], "little")
        comment_len = int.from_bytes(data[idx + 32:idx + 34], "little")
        entry_name = bytes(data[idx + 46:idx + 46 + name_len])
        if entry_name == name_b:
            flag_offset = idx + 8
            flags = int.from_bytes(data[flag_offset:flag_offset + 2], "little")
            flags |= bit
            data[flag_offset:flag_offset + 2] = flags.to_bytes(2, "little")
            return bytes(data)
        idx += 46 + name_len + extra_len + comment_len


def _make_zip(entries: dict[str, bytes], *, encrypted_names: set[str] | None = None) -> bytes:
    """Build a zip in memory with real ``zipfile`` writes (not a mock)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    zip_bytes = buf.getvalue()
    for name in encrypted_names or set():
        zip_bytes = _patch_central_dir_flag_bit(zip_bytes, name)
    return zip_bytes


def test_happy_path_two_files_become_two_children_sharing_container_family():
    zip_bytes = _make_zip({"a.txt": b"hello world", "b.csv": b"col1,col2\n1,2\n"})

    docs = build_zip_documents(
        zip_bytes,
        container_control="PREFIX 000001",
        production_id=1,
        source_path="uploads/bundle.zip",
        custodian="Alice",
    )

    assert len(docs) == 3
    container, child_a, child_b = docs

    assert container.file_type == "zip"
    assert container.text_content is None
    assert container.extraction_status == "ok"
    assert container.extraction_error is None
    assert container.family_id == "PREFIX 000001"
    assert container.bates_begin == "PREFIX 000001"

    assert child_a.file_name == "a.txt"
    assert child_a.family_id == "PREFIX 000001"
    assert child_a.bates_begin == "PREFIX 000001 .0001"
    assert child_a.text_content == "hello world"
    assert child_a.custodian == "Alice"
    assert child_a.extraction_status == "ok"

    assert child_b.file_name == "b.csv"
    assert child_b.family_id == "PREFIX 000001"
    assert child_b.bates_begin == "PREFIX 000001 .0002"
    assert "col1,col2" in child_b.text_content


def test_entry_cap_keeps_first_500_children_and_marks_container_partial():
    entries = {f"file{i:04d}.txt": f"body {i}".encode() for i in range(501)}
    zip_bytes = _make_zip(entries)

    docs = build_zip_documents(
        zip_bytes,
        container_control="PREFIX 000002",
        production_id=1,
        source_path="uploads/big.zip",
        custodian=None,
    )

    container = docs[0]
    children = docs[1:]
    assert len(children) == 500
    assert container.extraction_status == "partial"
    assert container.extraction_error
    assert "500" in container.extraction_error
    assert all(d.family_id == "PREFIX 000002" for d in children)


def test_path_traversal_entry_is_skipped_and_noted():
    zip_bytes = _make_zip({"../evil.txt": b"nope", "ok.txt": b"fine"})

    docs = build_zip_documents(
        zip_bytes,
        container_control="PREFIX 000003",
        production_id=1,
        source_path="uploads/traversal.zip",
        custodian=None,
    )

    container = docs[0]
    children = docs[1:]
    assert len(children) == 1
    assert children[0].file_name == "ok.txt"
    assert container.extraction_status == "partial"
    assert "traversal" in container.extraction_error
    assert "evil.txt" in container.extraction_error


def test_encrypted_entry_is_skipped_with_note():
    zip_bytes = _make_zip(
        {"secret.txt": b"shh", "open.txt": b"hi"},
        encrypted_names={"secret.txt"},
    )

    docs = build_zip_documents(
        zip_bytes,
        container_control="PREFIX 000004",
        production_id=1,
        source_path="uploads/enc.zip",
        custodian=None,
    )

    container = docs[0]
    children = docs[1:]
    assert len(children) == 1
    assert children[0].file_name == "open.txt"
    assert container.extraction_status == "partial"
    assert "encrypted" in container.extraction_error
    assert "secret.txt" in container.extraction_error


def test_nested_zip_children_join_outermost_container_family():
    inner_zip = _make_zip({"inner_a.txt": b"inner body"})
    outer_zip = _make_zip({"outer_a.txt": b"outer body", "nested.zip": inner_zip})

    docs = build_zip_documents(
        outer_zip,
        container_control="PREFIX 000005",
        production_id=1,
        source_path="uploads/nested.zip",
        custodian=None,
    )

    container = docs[0]
    children = docs[1:]
    names = {d.file_name for d in children}
    assert names == {"outer_a.txt", "inner_a.txt"}
    # No separate Document row was created for the nested zip itself.
    assert all(d.file_type != "zip" for d in children)
    assert all(d.family_id == "PREFIX 000005" for d in children)
    assert container.extraction_status == "ok"


def test_zip_beyond_depth_guard_ingests_as_single_unsupported_child():
    depth3 = _make_zip({"leaf.txt": b"too deep"})
    depth2 = _make_zip({"depth3.zip": depth3})
    depth1 = _make_zip({"depth2.zip": depth2})

    docs = build_zip_documents(
        depth1,
        container_control="PREFIX 000006",
        production_id=1,
        source_path="uploads/deep.zip",
        custodian=None,
    )

    children = docs[1:]
    assert len(children) == 1
    assert children[0].file_name == "depth3.zip"
    assert children[0].file_type == "zip"
    assert children[0].extraction_status == "unsupported"


def test_corrupt_zip_becomes_single_error_container_row():
    docs = build_zip_documents(
        b"not a zip file at all",
        container_control="PREFIX 000007",
        production_id=1,
        source_path="uploads/broken.zip",
        custodian=None,
    )

    assert len(docs) == 1
    assert docs[0].extraction_status == "error"
    assert docs[0].file_type == "zip"
    assert docs[0].extraction_error


def test_absolute_path_entry_is_skipped_and_noted():
    zip_bytes = _make_zip({"/etc/evil.txt": b"nope", "C:/also/evil.txt": b"nope2", "ok.txt": b"fine"})

    docs = build_zip_documents(
        zip_bytes,
        container_control="PREFIX 000008",
        production_id=1,
        source_path="uploads/absolute.zip",
        custodian=None,
    )

    container = docs[0]
    children = docs[1:]
    assert len(children) == 1
    assert children[0].file_name == "ok.txt"
    assert container.extraction_status == "partial"
    assert "traversal" in container.extraction_error
    assert "evil.txt" in container.extraction_error


# --- Finding 1: bounded decompression reads (bomb-proofing) ------------------
#
# CPython's zipfile happens to bound ZipExtFile.read() to info.file_size (and
# raises BadZipFile on a CRC mismatch if a header lies about that size), but
# _ZipExploder must not depend on that stdlib implementation detail — the
# guard has to be enforced from actual bytes read, independent of what the
# header claims. _read_bounded ignores info.file_size entirely, so it is
# tested directly here for that exact property, plus an integration test
# through build_zip_documents confirming the skip-with-note plumbing.


def test_read_bounded_stops_at_limit_regardless_of_header_claim():
    """The bounded read must never surface more than `limit` bytes, and must
    return None (not a truncated/partial buffer) once the true decompressed
    size exceeds it — this is the guard itself, independent of any header
    field."""
    payload = b"\x00" * (5 * 1024 * 1024)
    zip_bytes = _make_zip({"big.bin": payload})
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    info = zf.getinfo("big.bin")
    assert info.file_size == len(payload)  # honestly labeled; irrelevant to _read_bounded

    exploder = ingest_native_mod._ZipExploder()

    # A limit far below the true size: the entry never lands in memory whole.
    assert exploder._read_bounded(zf, info, limit=1024) is None

    # A limit at/above the true size returns the exact real bytes.
    assert exploder._read_bounded(zf, info, limit=len(payload)) == payload


def test_oversized_entry_skipped_with_note_and_never_lands_as_a_document(monkeypatch):
    """Integration check: with the per-entry cap lowered, a genuinely large
    entry is skipped (with a note) and produces no child document at all —
    the bounded read, not a trusted header claim, is what stops it."""
    monkeypatch.setattr(ingest_native_mod, "_ZIP_MAX_ENTRY_BYTES", 1024)

    big_payload = b"\x00" * (5 * 1024 * 1024)  # compresses to a few KB; decompressed is 5MB
    zip_bytes = _make_zip({"huge.bin": big_payload, "small.txt": b"tiny"})

    docs = build_zip_documents(
        zip_bytes,
        container_control="PREFIX 000012",
        production_id=1,
        source_path="uploads/huge.zip",
        custodian=None,
    )

    container = docs[0]
    children = docs[1:]
    assert len(children) == 1
    assert children[0].file_name == "small.txt"
    assert all(d.file_name != "huge.bin" for d in docs)
    assert container.extraction_status == "partial"
    assert "huge.bin" in container.extraction_error
    assert "exceeds" in container.extraction_error


def _tiny_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "hello from zip pdf")
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_entry_in_zip_renders_via_pdf_path_and_joins_container_family(monkeypatch):
    """Finding 2(a): a PDF found inside a zip must go through the SAME
    page-render path as a top-level PDF (child doc with page_count set) and
    join the container's family."""
    uploaded: list[str] = []
    monkeypatch.setattr(
        storage_mod,
        "upload_bytes",
        lambda data, remote, content_type=None: uploaded.append(remote) or remote,
    )

    zip_bytes = _make_zip({"doc.pdf": _tiny_pdf_bytes()})

    docs = build_zip_documents(
        zip_bytes,
        container_control="PREFIX 000013",
        production_id=1,
        source_path="uploads/withpdf.zip",
        custodian="Alice",
        ocr_fn=lambda jpeg_bytes: "",
    )

    container = docs[0]
    children = docs[1:]
    assert len(children) == 1
    child = children[0]
    assert child.file_type == "pdf"
    assert child.file_name == "doc.pdf"
    assert child.page_count == 1
    assert "hello from zip pdf" in (child.text_content or "")
    assert child.extraction_status == "ok"
    assert child.family_id == container.family_id == "PREFIX 000013"
    assert len(uploaded) == 1
    assert container.extraction_status == "ok"


def _tiny_eml_bytes(subject: str = "Zip Test", body: str = "hello from eml") -> bytes:
    return (
        "From: a@example.com\r\n"
        "To: b@example.com\r\n"
        f"Subject: {subject}\r\n"
        "Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        "Message-ID: <abc@example.com>\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


def test_email_entry_in_zip_force_reassigns_family_to_zip_container():
    """Finding 2(b): an email container found inside a zip expands like a
    top-level email, but every resulting doc's family_id must be
    FORCE-REASSIGNED to the zip's family (not the message's own control
    number, which is what build_email_documents would set by default)."""
    zip_bytes = _make_zip({"message.eml": _tiny_eml_bytes()})

    docs = build_zip_documents(
        zip_bytes,
        container_control="PREFIX 000014",
        production_id=1,
        source_path="uploads/withemail.zip",
        custodian=None,
        ocr_fn=lambda jpeg_bytes: "",
    )

    container = docs[0]
    children = docs[1:]
    assert len(children) == 1
    child = children[0]
    assert child.file_type == "email"
    assert child.email_subject == "Zip Test"
    # NOT "PREFIX 000014 .0001" (the message's own control number) —
    # force-reassigned to the zip container's family.
    assert child.family_id == container.family_id == "PREFIX 000014"
    assert container.extraction_status == "ok"
