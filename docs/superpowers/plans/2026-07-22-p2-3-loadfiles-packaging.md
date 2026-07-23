# P2-3 Load Files + Manifest + Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package a rendered production set into a deliverable ZIP: Concordance DAT + doc-level OPT, per-artifact-hashed manifest with Bates-continuity validation, PDFs and privilege-safe text.

**Architecture:** Pure writers in `app/services/loadfile_export.py` (round-trip-tested against the import parsers); DB/storage assembly in `app/services/production_export.py`; one packaging Cloud Task (`enqueue_package`) with BackgroundTasks fallback; `package_status` state machine on `production_sets`.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, zipfile/tempfile, google-cloud-tasks, pytest fake-session tests.

**Spec:** `docs/superpowers/specs/2026-07-22-p2-3-loadfiles-packaging-design.md`

## Global Constraints

- Branch `feat/p2-3-loadfiles-packaging` (stacked on P2-2; PR base = `feat/p2-2-endorsement-rendering`). Verify branch before every commit.
- Migration `c9d0e1f2a3b4`, `down_revision = "b8c9d0e1f2a3"`, no `app.*` imports.
- Package statuses (exact): `not_started`, `packaging`, `packaged`, `error`.
- DAT/OPT conventions IDENTICAL to `app/utils/parsers.py`: `þ` (þ) wrapper, DC4 (\x14) separator, UTF-8 BOM, CRLF. Round-trip tests parse our bytes with `parse_dat`/`parse_opt`.
- Privilege safety: withheld rows blank SUBJECT/FILENAME; TEXT ships ONLY for `produce` docs (stored text is pre-redaction).
- Workers return 200 on assembly failures; errors land in `package_status="error"`.
- `compute_manifest(ps, items, artifact_hashes=None)` is sync and DB-free (plan supersedes the spec's async sketch).
- Tests fake-session, 0 warnings. No AI-attribution trailers on commits/PR.

---

### Task 1: Migration + model columns

**Files:**
- Create: `backend/alembic/versions/c9d0e1f2a3b4_add_package_state.py`
- Modify: `backend/app/models.py` (`ProductionSet` after `rendered_at`)

**Interfaces:** Produces `ProductionSet.package_status/package_error/package_path/packaged_at`.

- [ ] **Step 1: Migration**

```python
"""add production-set package state

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("production_sets", sa.Column("package_status", sa.String(length=20), nullable=False, server_default=sa.text("'not_started'")))
    op.add_column("production_sets", sa.Column("package_error", sa.Text(), nullable=True))
    op.add_column("production_sets", sa.Column("package_path", sa.String(length=500), nullable=True))
    op.add_column("production_sets", sa.Column("packaged_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("production_sets", "packaged_at")
    op.drop_column("production_sets", "package_path")
    op.drop_column("production_sets", "package_error")
    op.drop_column("production_sets", "package_status")
```

- [ ] **Step 2: Model columns** — in `ProductionSet` after `rendered_at`:

```python
    # P2-3 — package state
    package_status = Column(String(20), nullable=False, default="not_started")  # not_started|packaging|packaged|error
    package_error = Column(Text, nullable=True)
    package_path = Column(String(500), nullable=True)
    packaged_at = Column(DateTime, nullable=True)
```

- [ ] **Step 3: Verify** — py_compile the migration; `import app.models`; grep `b8c9d0e1f2a3` → exactly two hits; no `app.` imports in migration.

- [ ] **Step 4: Commit** — `git add` both, message `feat(p2-3): package-state columns on production sets`.

---

### Task 2: Pure load-file writers + round-trip tests

**Files:**
- Create: `backend/app/services/loadfile_export.py`
- Test: `backend/tests/test_loadfile_export.py`

**Interfaces:**
- Consumes: `parse_dat`/`parse_opt` (tests only).
- Produces (Task 3 imports): `DAT_COLUMNS`, `dat_bytes(rows) -> bytes`, `opt_bytes(entries) -> bytes` (entries = `(bates, volume, path, pages)`), `check_continuity(items, prefix, start_number) -> list[str]` (items = `(begin, end, pages)` in sort order), `manifest_dict(ps_info, counts, bates_range, continuity_errors, artifacts) -> dict`.

- [ ] **Step 1: Failing tests** — `backend/tests/test_loadfile_export.py`:

```python
"""Round-trip tests: our DAT/OPT writers vs our own import parsers (P2-3)."""

from app.services.loadfile_export import (
    DAT_COLUMNS,
    check_continuity,
    dat_bytes,
    manifest_dict,
    opt_bytes,
)
from app.utils.parsers import parse_dat, parse_opt


def test_dat_round_trips_through_importer(tmp_path):
    rows = [{c: f"v {c}" for c in DAT_COLUMNS}]
    rows[0]["BEGBATES"] = "SMITH000001"
    p = tmp_path / "out.dat"
    p.write_bytes(dat_bytes(rows))
    parsed = parse_dat(str(p))
    assert len(parsed) == 1
    assert parsed[0]["BEGBATES"] == "SMITH000001"
    assert set(parsed[0]) == set(DAT_COLUMNS)
    assert parsed[0]["CUSTODIAN"] == "v CUSTODIAN"


def test_dat_bytes_format():
    data = dat_bytes([{"BEGBATES": "A1"}])
    assert data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = data.decode("utf-8-sig")
    lines = text.split("\r\n")
    assert lines[0].startswith("þBEGBATESþ\x14")
    assert f"þA1þ" in lines[1]
    assert text.endswith("\r\n")


def test_dat_missing_keys_become_empty():
    data = dat_bytes([{"BEGBATES": "A1"}])
    row = data.decode("utf-8-sig").split("\r\n")[1]
    assert len(row.split("\x14")) == len(DAT_COLUMNS)
    assert "þþ" in row  # empty wrapped field


def test_dat_strips_control_chars():
    data = dat_bytes([{"SUBJECT": "bad\x14value\r\nhereþ!"}])
    row = data.decode("utf-8-sig").split("\r\n")[1]
    assert len(row.split("\x14")) == len(DAT_COLUMNS)


def test_opt_round_trips_through_importer(tmp_path):
    entries = [("SMITH000001", "VOL001", ".\\PDFS\\SMITH000001.pdf", 3),
               ("SMITH000004", "VOL001", ".\\PDFS\\SMITH000004.pdf", 1)]
    p = tmp_path / "out.opt"
    p.write_bytes(opt_bytes(entries))
    parsed = parse_opt(str(p))
    assert parsed == {
        "SMITH000001": ["./PDFS/SMITH000001.pdf"],
        "SMITH000004": ["./PDFS/SMITH000004.pdf"],
    }


def test_opt_line_shape():
    data = opt_bytes([("A1", "VOL001", ".\\PDFS\\A1.pdf", 5)])
    assert data == b"A1,VOL001,.\\PDFS\\A1.pdf,Y,,,5\r\n"


def test_continuity_clean():
    items = [("P000001", "P000003", 3), ("P000004", "P000004", 1)]
    assert check_continuity(items, "P", 1) == []


def test_continuity_catches_gap_overlap_end_and_start():
    assert check_continuity([("P000002", "P000002", 1)], "P", 1)      # wrong start
    assert check_continuity([("P000001", "P000003", 2)], "P", 1)      # wrong end
    items = [("P000001", "P000002", 2), ("P000005", "P000005", 1)]
    assert check_continuity(items, "P", 1)                            # gap
    items = [("P000001", "P000002", 2), ("P000002", "P000002", 1)]
    assert check_continuity(items, "P", 1)                            # overlap
    assert check_continuity([], "P", 1) == ["production set has no members"]


def test_manifest_dict_shape():
    m = manifest_dict({"id": 1}, {"documents": 2}, {"begin": "A1", "end": "A2"},
                      [], [{"bates_begin": "A1"}])
    assert m["continuity"] == {"ok": True, "errors": []}
    assert m["production_set"] == {"id": 1}
    assert "generated_at" in m
    m2 = manifest_dict({}, {}, {}, ["gap"], [])
    assert m2["continuity"]["ok"] is False
```

- [ ] **Step 2: Verify fail** — ModuleNotFoundError.

- [ ] **Step 3: Implement** — `backend/app/services/loadfile_export.py`:

```python
"""Pure Concordance DAT / Opticon OPT writers + manifest helpers (P2-3).

These mirror the import conventions in app/utils/parsers.py exactly — the
import parsers are the round-trip oracle for this module's output.
"""

from __future__ import annotations

from datetime import datetime, timezone

FIELD_WRAPPER = "þ"   # þ
FIELD_SEPARATOR = "\x14"   # DC4

DAT_COLUMNS = ["BEGBATES", "ENDBATES", "BEGATTACH", "ENDATTACH", "CUSTODIAN",
               "FROM", "TO", "CC", "DATESENT", "DATERECEIVED", "SUBJECT",
               "FILENAME", "FILETYPE", "MD5HASH", "SHA256HASH", "PAGECOUNT",
               "REDACTED", "WITHHELD", "CONFIDENTIALITY", "TEXTPATH"]


def _clean(value) -> str:
    """Format-breaking characters never legitimately appear in metadata."""
    if value is None:
        return ""
    s = str(value)
    for ch in (FIELD_WRAPPER, FIELD_SEPARATOR, "\r", "\n"):
        s = s.replace(ch, " ")
    return s.strip()


def dat_bytes(rows: list[dict]) -> bytes:
    def fmt(values):
        return FIELD_SEPARATOR.join(
            f"{FIELD_WRAPPER}{v}{FIELD_WRAPPER}" for v in values)

    lines = [fmt(DAT_COLUMNS)]
    for row in rows:
        lines.append(fmt(_clean(row.get(c)) for c in DAT_COLUMNS))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def opt_bytes(entries: list[tuple[str, str, str, int]]) -> bytes:
    lines = [f"{bates},{volume},{path},Y,,,{pages}"
             for bates, volume, path, pages in entries]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def check_continuity(items: list[tuple[str, str, int]], prefix: str,
                     start_number: int) -> list[str]:
    """items = (bates_begin, bates_end, pages) in sort order. [] = gap-free."""
    if not items:
        return ["production set has no members"]
    errors = []
    expected = start_number
    for begin, end, pages in items:
        b, e = int(begin[len(prefix):]), int(end[len(prefix):])
        if b != expected:
            errors.append(f"{begin}: expected to start at {prefix}{expected} (gap or overlap)")
        if e != b + pages - 1:
            errors.append(f"{begin}: end {end} does not match {pages} page(s)")
        expected = e + 1
    return errors


def manifest_dict(ps_info: dict, counts: dict, bates_range: dict,
                  continuity_errors: list[str], artifacts: list[dict]) -> dict:
    return {
        "production_set": ps_info,
        "counts": counts,
        "bates_range": bates_range,
        "continuity": {"ok": not continuity_errors, "errors": list(continuity_errors)},
        "artifacts": artifacts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
```

- [ ] **Step 4: Verify pass** — 9 passed, 0 warnings.
- [ ] **Step 5: Commit** — `feat(p2-3): DAT/OPT writers + continuity check (round-trip tested vs importers)`.

---

### Task 3: Export assembly service

**Files:**
- Create: `backend/app/services/production_export.py`
- Test: `backend/tests/test_production_export.py`

**Interfaces:**
- Consumes: Task 2 writers, models, `storage`.
- Produces (Task 4 imports): `package_path_for(ps) -> str`, `build_dat_rows(db, ps, items) -> list[dict]`, `compute_manifest(ps, items, artifact_hashes=None) -> dict` (sync), `package_set(db, set_id) -> None`.

- [ ] **Step 1: Failing tests** — `backend/tests/test_production_export.py`:

```python
"""Fake-session tests for production export assembly (P2-3). No DB/GCS."""

import asyncio
import io
import zipfile
from uuid import uuid4

import app.services.production_export as pe
from tests.fakes import TS, FakeResult, FakeSession


class FakePS:
    def __init__(self, **kw):
        self.id = kw.get("set_id", 1)
        self.production_id = kw.get("production_id", 1)
        self.name = "Vol 1"
        self.status = kw.get("status", "locked")
        self.prefix = kw.get("prefix", "SMITH")
        self.padding = 6
        self.start_number = kw.get("start_number", 1)
        self.designation = kw.get("designation", None)
        self.locked_at = TS
        self.render_status = kw.get("render_status", "rendered")
        self.rendered_at = TS
        self.package_status = kw.get("package_status", "packaging")
        self.package_error = None
        self.package_path = None
        self.packaged_at = None


class FakeItem:
    def __init__(self, document_id, bates_begin, bates_end, pages,
                 disposition="produce", **kw):
        self.document_id = document_id
        self.bates_begin = bates_begin
        self.bates_end = bates_end
        self.pages = pages
        self.disposition = disposition
        self.designation = kw.get("designation", None)
        self.output_path = kw.get("output_path", f"productions/1/x/{bates_begin}.pdf")
        self.sort_order = kw.get("sort_order", 1)


class FakeDoc:
    def __init__(self, doc_id, **kw):
        self.id = doc_id
        self.family_id = kw.get("family_id", None)
        self.custodian = kw.get("custodian", "T. Owner")
        self.email_from = kw.get("email_from", "a@x.com")
        self.email_to = kw.get("email_to", "b@y.com")
        self.email_cc = kw.get("email_cc", None)
        self.email_subject = kw.get("email_subject", "Secret subject")
        self.date_sent = kw.get("date_sent", TS)
        self.date_received = None
        self.file_name = kw.get("file_name", "mail.eml")
        self.file_type = kw.get("file_type", "eml")
        self.file_hash_md5 = "md5x"
        self.file_hash_sha256 = "shax"
        self.text_content = kw.get("text_content", "hello text")


def test_package_path_for():
    assert pe.package_path_for(FakePS()) == \
        "productions/1/production_sets/1/package/SMITH_production.zip"


def test_build_dat_rows_values_and_family_ranges():
    d1, d2, d3 = uuid4(), uuid4(), uuid4()
    items = [
        FakeItem(d1, "SMITH000001", "SMITH000002", 2, "produce", sort_order=1),
        FakeItem(d2, "SMITH000003", "SMITH000003", 1, "redact_in_part", sort_order=2),
        FakeItem(d3, "SMITH000004", "SMITH000004", 1, "withhold", sort_order=3),
    ]
    docs = [FakeDoc(d1, family_id="F1"), FakeDoc(d2, family_id="F1"),
            FakeDoc(d3)]
    db = FakeSession(responders=[("FROM documents", FakeResult(items=docs))])
    rows = asyncio.run(pe.build_dat_rows(db, FakePS(designation="CONF"), items))
    r1, r2, r3 = rows
    # family F1 spans docs 1-2
    assert (r1["BEGATTACH"], r1["ENDATTACH"]) == ("SMITH000001", "SMITH000003")
    assert (r2["BEGATTACH"], r2["ENDATTACH"]) == ("SMITH000001", "SMITH000003")
    assert (r3["BEGATTACH"], r3["ENDATTACH"]) == ("SMITH000004", "SMITH000004")
    assert r1["TEXTPATH"] == ".\\TEXT\\SMITH000001.txt"
    assert r2["TEXTPATH"] == ""            # redacted: never ship stored text
    assert r2["REDACTED"] == "Y"
    assert r3["WITHHELD"] == "Y"
    assert r3["SUBJECT"] == "" and r3["FILENAME"] == ""  # privilege safety
    assert r1["SUBJECT"] == "Secret subject"
    assert r1["CONFIDENTIALITY"] == "CONF"
    assert r1["DATESENT"] == "2026-07-22"


def test_compute_manifest_counts_and_continuity():
    d1, d2 = uuid4(), uuid4()
    items = [FakeItem(d1, "SMITH000001", "SMITH000002", 2, "produce"),
             FakeItem(d2, "SMITH000005", "SMITH000005", 1, "withhold")]
    m = pe.compute_manifest(FakePS(), items)
    assert m["counts"] == {"documents": 2, "pages": 3, "produce": 1,
                           "redact_in_part": 0, "withhold": 1}
    assert m["bates_range"] == {"begin": "SMITH000001", "end": "SMITH000005"}
    assert m["continuity"]["ok"] is False  # gap 000003-000004
    assert m["artifacts"][0]["path"].endswith("SMITH000001.pdf")


def test_package_set_happy_path(monkeypatch):
    d1, d2 = uuid4(), uuid4()
    items = [FakeItem(d1, "SMITH000001", "SMITH000001", 1, "produce", sort_order=1),
             FakeItem(d2, "SMITH000002", "SMITH000002", 1, "withhold", sort_order=2)]
    docs = [FakeDoc(d1), FakeDoc(d2)]
    ps = FakePS()
    captured = {}

    def fake_download(path):
        return b"%PDF-fake"

    def fake_upload(local_path, remote_path, content_type=None):
        with open(local_path, "rb") as f:
            captured["zip"] = f.read()
        captured["remote"] = (remote_path, content_type)
        return remote_path

    monkeypatch.setattr(pe.storage, "get_download_bytes", fake_download)
    monkeypatch.setattr(pe.storage, "upload_file", fake_upload)
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("FROM production_set_items", FakeResult(items=items)),
            ("FROM documents", FakeResult(items=docs)),
        ],
    )
    asyncio.run(pe.package_set(db, 1))
    assert ps.package_status == "packaged"
    assert ps.package_path == pe.package_path_for(ps)
    zf = zipfile.ZipFile(io.BytesIO(captured["zip"]))
    names = set(zf.namelist())
    assert names == {"DATA/SMITH.dat", "DATA/SMITH.opt",
                     "PDFS/SMITH000001.pdf", "PDFS/SMITH000002.pdf",
                     "TEXT/SMITH000001.txt", "manifest.json"}
    manifest = zf.read("manifest.json").decode()
    assert "sha256" in manifest
    assert captured["remote"] == (ps.package_path, "application/zip")


def test_package_set_missing_artifact_marks_error(monkeypatch):
    d1 = uuid4()
    items = [FakeItem(d1, "SMITH000001", "SMITH000001", 1, "produce")]
    ps = FakePS()

    def boom(path):
        raise RuntimeError("404 from GCS")

    monkeypatch.setattr(pe.storage, "get_download_bytes", boom)
    monkeypatch.setattr(pe.storage, "upload_file", lambda *a, **k: None)
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("FROM production_set_items", FakeResult(items=items)),
            ("FROM documents", FakeResult(items=[FakeDoc(d1)])),
        ],
    )
    asyncio.run(pe.package_set(db, 1))
    assert ps.package_status == "error"
    assert "SMITH000001" in ps.package_error


def test_package_set_requires_rendered():
    ps = FakePS(render_status="rendering")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    asyncio.run(pe.package_set(db, 1))
    assert ps.package_status == "error"
```

- [ ] **Step 2: Verify fail** — ModuleNotFoundError.

- [ ] **Step 3: Implement** — `backend/app/services/production_export.py`:

```python
"""Assemble the deliverable package for a rendered production set (P2-3)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, ProductionSet, ProductionSetItem
from app.services import storage
from app.services.loadfile_export import (
    check_continuity,
    dat_bytes,
    manifest_dict,
    opt_bytes,
)

logger = logging.getLogger(__name__)


def package_path_for(ps: ProductionSet) -> str:
    return (f"productions/{ps.production_id}/production_sets/{ps.id}/"
            f"package/{ps.prefix}_production.zip")


def _volume(ps: ProductionSet) -> str:
    return f"VOL{ps.id:03d}"


async def _docs_by_id(db: AsyncSession, items) -> dict:
    docs = (await db.execute(
        select(Document).where(Document.id.in_([i.document_id for i in items]))
    )).scalars().all()
    return {d.id: d for d in docs}


def _family_ranges(items, docs) -> dict:
    """family_id -> [min begin, max end]. Items arrive in sort_order and lock
    ordering keeps families contiguous, so first/last occurrence bound the range."""
    ranges: dict = {}
    for item in items:
        doc = docs.get(item.document_id)
        fam = doc.family_id if doc is not None else None
        if not fam:
            continue
        if fam not in ranges:
            ranges[fam] = [item.bates_begin, item.bates_end]
        else:
            ranges[fam][1] = item.bates_end
    return ranges


async def build_dat_rows(db: AsyncSession, ps: ProductionSet, items) -> list[dict]:
    docs = await _docs_by_id(db, items)
    fam_ranges = _family_ranges(items, docs)
    rows = []
    for item in items:
        doc = docs.get(item.document_id)
        withheld = item.disposition == "withhold"
        fam = doc.family_id if doc is not None else None
        if fam and fam in fam_ranges:
            beg_att, end_att = fam_ranges[fam]
        else:
            beg_att, end_att = item.bates_begin, item.bates_end
        has_text = (item.disposition == "produce" and doc is not None
                    and bool(doc.text_content))
        rows.append({
            "BEGBATES": item.bates_begin,
            "ENDBATES": item.bates_end,
            "BEGATTACH": beg_att,
            "ENDATTACH": end_att,
            "CUSTODIAN": getattr(doc, "custodian", None),
            "FROM": getattr(doc, "email_from", None),
            "TO": getattr(doc, "email_to", None),
            "CC": getattr(doc, "email_cc", None),
            "DATESENT": doc.date_sent.date().isoformat()
                        if doc is not None and doc.date_sent else "",
            "DATERECEIVED": doc.date_received.date().isoformat()
                            if doc is not None and doc.date_received else "",
            # Privilege safety: withheld rows carry log-equivalent metadata only.
            "SUBJECT": "" if withheld else getattr(doc, "email_subject", None),
            "FILENAME": "" if withheld else getattr(doc, "file_name", None),
            "FILETYPE": getattr(doc, "file_type", None),
            "MD5HASH": getattr(doc, "file_hash_md5", None),
            "SHA256HASH": getattr(doc, "file_hash_sha256", None),
            "PAGECOUNT": item.pages,
            "REDACTED": "Y" if item.disposition == "redact_in_part" else "N",
            "WITHHELD": "Y" if withheld else "N",
            "CONFIDENTIALITY": item.designation or ps.designation or "",
            "TEXTPATH": f".\\TEXT\\{item.bates_begin}.txt" if has_text else "",
        })
    return rows


def compute_manifest(ps: ProductionSet, items, artifact_hashes: dict | None = None) -> dict:
    counts = {"documents": len(items), "pages": sum(i.pages or 0 for i in items),
              "produce": 0, "redact_in_part": 0, "withhold": 0}
    for i in items:
        if i.disposition in counts:
            counts[i.disposition] += 1
    bates_range = ({"begin": items[0].bates_begin, "end": items[-1].bates_end}
                   if items else {"begin": None, "end": None})
    errors = check_continuity(
        [(i.bates_begin, i.bates_end, i.pages or 0) for i in items],
        ps.prefix, ps.start_number)
    artifacts = []
    for i in items:
        entry = {"bates_begin": i.bates_begin, "path": i.output_path}
        if artifact_hashes and i.bates_begin in artifact_hashes:
            entry.update(artifact_hashes[i.bates_begin])
        artifacts.append(entry)
    ps_info = {"id": ps.id, "name": ps.name, "prefix": ps.prefix,
               "designation": ps.designation,
               "locked_at": ps.locked_at.isoformat() if ps.locked_at else None,
               "rendered_at": ps.rendered_at.isoformat() if ps.rendered_at else None}
    return manifest_dict(ps_info, counts, bates_range, errors, artifacts)


async def package_set(db: AsyncSession, set_id: int) -> None:
    """Packaging job body. Trigger endpoint already set package_status='packaging'.
    Failures land in package_status='error'; never raises (worker returns 200)."""
    ps = await db.get(ProductionSet, set_id)
    if not ps:
        return
    try:
        if ps.status != "locked" or ps.render_status != "rendered":
            raise RuntimeError("Production set is not rendered")
        items = (await db.execute(
            select(ProductionSetItem)
            .where(ProductionSetItem.production_set_id == set_id)
            .order_by(ProductionSetItem.sort_order)
        )).scalars().all()
        if not items:
            raise RuntimeError("Production set has no members")
        docs = await _docs_by_id(db, items)
        dat_rows = await build_dat_rows(db, ps, items)
        volume = _volume(ps)
        opt_entries = [(i.bates_begin, volume, f".\\PDFS\\{i.bates_begin}.pdf",
                        i.pages or 0) for i in items]

        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            hashes: dict = {}
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"DATA/{ps.prefix}.dat", dat_bytes(dat_rows))
                zf.writestr(f"DATA/{ps.prefix}.opt", opt_bytes(opt_entries))
                for item in items:
                    if not item.output_path:
                        raise RuntimeError(
                            f"Missing rendered artifact for {item.bates_begin}")
                    try:
                        pdf = storage.get_download_bytes(item.output_path)
                    except Exception as exc:
                        raise RuntimeError(
                            f"Could not fetch artifact for {item.bates_begin}: {exc}")
                    hashes[item.bates_begin] = {
                        "sha256": hashlib.sha256(pdf).hexdigest(),
                        "bytes": len(pdf),
                    }
                    zf.writestr(f"PDFS/{item.bates_begin}.pdf", pdf)
                    doc = docs.get(item.document_id)
                    if (item.disposition == "produce" and doc is not None
                            and doc.text_content):
                        zf.writestr(f"TEXT/{item.bates_begin}.txt",
                                    doc.text_content.encode("utf-8"))
                manifest = compute_manifest(ps, items, hashes)
                zf.writestr("manifest.json",
                            json.dumps(manifest, indent=2).encode("utf-8"))
            path = package_path_for(ps)
            storage.upload_file(tmp_path, path, "application/zip")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        ps.package_status = "packaged"
        ps.package_error = None
        ps.package_path = path
        ps.packaged_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
    except Exception as exc:
        logger.exception("Packaging failed for set %s", set_id)
        ps.package_status = "error"
        ps.package_error = str(exc)
        await db.commit()
```

- [ ] **Step 4: Verify pass** — 6 passed, 0 warnings (plus Task 2's 9).
- [ ] **Step 5: Commit** — `feat(p2-3): package assembly — DAT/OPT/manifest/PDF/text zip with hashes`.

---

### Task 4: Task fan-out + endpoints + schema fields

**Files:**
- Modify: `backend/app/services/tasks.py` (append `enqueue_package`)
- Modify: `backend/app/schemas.py` (`ProductionSetOut` package fields)
- Modify: `backend/app/routers/production_sets.py` (imports, create-endpoint field, four endpoints)
- Test: `backend/tests/test_production_set_endpoints.py` (append; extend `FakePS`)

**Interfaces:**
- Produces: `GET .../manifest`, `POST .../package`, `POST /production-sets/package-worker` (OIDC), `GET .../package` (307). `ProductionSetOut` package fields.

- [ ] **Step 1: Extend FakePS + failing tests**

`FakePS.__init__` gains (after render fields):

```python
        self.package_status = kw.get("package_status", "not_started")
        self.package_error = None
        self.package_path = kw.get("package_path", None)
        self.packaged_at = None
```

Append tests:

```python
# --- manifest + packaging endpoints (P2-3) ---------------------------------

def test_manifest_requires_locked(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="draft")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.get_manifest(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_manifest_returns_continuity(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    item = FakeItem(d1, item_id=1, sort_order=1, bates_begin="SMITH000001",
                    bates_end="SMITH000002", pages=2, disposition="produce")
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS(status="locked")},
        responders=[("FROM production_set_items", FakeResult(items=[item]))],
    )
    out = asyncio.run(rps.get_manifest(set_id=1, db=db, user=FakeUser()))
    assert out["continuity"]["ok"] is True
    assert out["counts"]["documents"] == 1


def test_package_requires_rendered(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked", render_status="rendering")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.package_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_package_409_while_packaging(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked", render_status="rendered", package_status="packaging")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.package_production_set(
            set_id=1, background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_package_trigger_fallback(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(rps.tasks, "is_configured", lambda: False)
    ps = FakePS(status="locked", render_status="rendered")
    bg = FakeBackgroundTasks()
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("count", FakeResult(scalar=4))],
    )
    out = asyncio.run(rps.package_production_set(
        set_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert out == {"documents": 4}
    assert ps.package_status == "packaging"
    assert len(bg.tasks) == 1


def test_package_worker_delegates(monkeypatch):
    called = {}

    async def fake_package_set(db, set_id):
        called["set"] = set_id

    monkeypatch.setattr(rps, "package_set", fake_package_set)
    out = asyncio.run(rps.package_worker_handler(
        body={"set_id": 5}, db=FakeSession(), _verified=None))
    assert out == {"ok": True}
    assert called == {"set": 5}


def test_package_download_404_until_packaged(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="locked")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.download_package(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_package_download_redirects(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(rps, "get_signed_url",
                        lambda path, **kw: f"https://signed.example/{path}")
    ps = FakePS(status="locked", package_status="packaged",
                package_path="productions/1/production_sets/1/package/SMITH_production.zip")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    out = asyncio.run(rps.download_package(set_id=1, db=db, user=FakeUser()))
    assert out.status_code == 307
    assert out.headers["location"].endswith("SMITH_production.zip")
```

- [ ] **Step 2: Verify fail** — AttributeError on `get_manifest`.

- [ ] **Step 3: Implement**

(a) `tasks.py` append:

```python
def enqueue_package(set_id: int) -> None:
    """Enqueue a Cloud Task to package one production set into its ZIP."""
    if not is_configured():
        raise RuntimeError("Cloud Tasks not configured")

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    handler_url = f"{settings.cloud_run_service_url}/api/production-sets/package-worker"
    payload = json.dumps({"set_id": set_id}).encode()

    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=handler_url,
            headers={"Content-Type": "application/json"},
            body=payload,
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.cloud_tasks_service_account,
                audience=settings.cloud_run_service_url,
            ),
        ),
        # Zipping a whole production (downloads + hashing) can run long.
        dispatch_deadline=duration_pb2.Duration(seconds=1800),
    )

    client.create_task(parent=queue_path, task=task)
    logger.info("Enqueued packaging for production set %d", set_id)
```

(b) `schemas.py` — `ProductionSetOut` after `rendered_count`:

```python
    package_status: str = "not_started"
    package_error: str | None = None
    package_path: str | None = None
    packaged_at: datetime | None = None
```

(c) `production_sets.py`:
- import: `from app.services.production_export import compute_manifest, package_set`
- `create_production_set`: add `package_status="not_started"` to the `ProductionSet(...)` constructor (same pre-flush-validation reason as `render_status`).
- Append endpoints:

```python
@router.get("/production-sets/{set_id}/manifest")
async def get_manifest(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    if ps.status != "locked":
        raise HTTPException(status_code=409, detail="Production set must be locked")
    items = (await db.execute(
        select(ProductionSetItem)
        .where(ProductionSetItem.production_set_id == set_id)
        .order_by(ProductionSetItem.sort_order)
    )).scalars().all()
    return compute_manifest(ps, items)


@router.post("/production-sets/{set_id}/package")
async def package_production_set(
    set_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "locked" or ps.render_status != "rendered":
        raise HTTPException(status_code=409, detail="Production set must be rendered before packaging")
    if ps.package_status == "packaging":
        raise HTTPException(status_code=409, detail="Packaging already in progress")
    count = (await db.execute(
        select(func.count(ProductionSetItem.id))
        .where(ProductionSetItem.production_set_id == set_id)
    )).scalar() or 0
    ps.package_status = "packaging"
    ps.package_error = None
    ps.package_path = None
    ps.packaged_at = None
    await log_action(db, user, "production_set_package_started", "production_set",
                     str(set_id), production_id=ps.production_id,
                     details={"documents": count})
    await db.commit()
    if tasks.is_configured():
        tasks.enqueue_package(set_id)
    else:
        background_tasks.add_task(_package_inline, set_id)
    return {"documents": count}


async def _package_inline(set_id: int):
    """Dev fallback: package in-process on a fresh session."""
    from app.database import async_session

    async with async_session() as db:
        await package_set(db, set_id)


@router.post("/production-sets/package-worker")
async def package_worker_handler(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker — packages one set. Always 200; failures land in
    package_status='error' (non-2xx would loop a deterministic failure)."""
    set_id = body.get("set_id")
    if set_id is None:
        raise HTTPException(status_code=400, detail="set_id required")
    await package_set(db, int(set_id))
    return {"ok": True}


@router.get("/production-sets/{set_id}/package")
async def download_package(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    if ps.package_status != "packaged" or not ps.package_path:
        raise HTTPException(status_code=404, detail="Package not available")
    url = get_signed_url(
        ps.package_path,
        response_disposition=f'attachment; filename="{ps.prefix}_production.zip"',
    )
    return RedirectResponse(url, status_code=307)
```

- [ ] **Step 4: Verify pass** — endpoint file total 46 (38 + 8), plus export/loadfile/endorse/render/numbering suites, 0 warnings.
- [ ] **Step 5: Commit** — `feat(p2-3): manifest + packaging endpoints with Cloud Tasks worker`.

---

### Task 5: Full-suite verification + PR

- [ ] **Step 1:** Full suite — only known `test_ai_review` failure allowed.
- [ ] **Step 2:** Grep `down_revision = "b8c9d0e1f2a3"` — exactly the new migration; no `app.` imports.
- [ ] **Step 3:** Push + PR:

```bash
git push -u origin feat/p2-3-loadfiles-packaging
gh pr create --base feat/p2-2-endorsement-rendering --title "feat(p2-3): DAT/OPT load files, manifest, ZIP packaging" --body "$(cat <<'EOF'
## Summary
- Concordance DAT + doc-level OPT writers mirroring the import parsers' conventions exactly (thorn wrapper, DC4 separator, UTF-8 BOM, CRLF) — round-trip tested against parse_dat/parse_opt
- Privilege-safe deliverable: withheld rows blank SUBJECT/FILENAME; TEXT ships only for produce docs (stored text is pre-redaction)
- Manifest with Bates-continuity validation (on-demand endpoint for instant checks + hashed copy inside the package)
- Packaging job (Cloud Task or dev fallback) streams DATA/PDFS/TEXT/manifest.json into a ZIP in GCS; per-artifact SHA-256; package_status state machine + signed-URL download

Stacked on #39 (P2-2). Spec: docs/superpowers/specs/2026-07-22-p2-3-loadfiles-packaging-design.md

## Test plan
- [x] Round-trip tests: our DAT/OPT parsed by our own importers; byte-level BOM/wrapper/separator/CRLF checks; continuity gap/overlap/end/start violations
- [x] Assembly tests: family attach ranges, withheld blanking, TEXTPATH gating, zip contents + hashes, missing-artifact error path, rendered prerequisite
- [x] Endpoint tests: manifest 409/content, package 409s + fallback, worker delegation, download 404/307
- [x] Full backend suite green (1 pre-existing unrelated failure)
EOF
)"
```
