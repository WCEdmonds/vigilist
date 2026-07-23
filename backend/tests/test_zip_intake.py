"""Unit tests for zip-container intake (Task F3). Pure, no DB/storage — real
in-memory zipfile fixtures, not mocks of the zipfile library."""

import io
import zipfile

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
