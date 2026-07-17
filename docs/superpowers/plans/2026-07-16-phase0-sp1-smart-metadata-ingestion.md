# Phase 0 · SP1 — Smart Metadata Ingestion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make load-file ingest smart and metadata-aware — auto-detect load-file format, map columns (alias dictionary + AI-assisted, human-confirmed), promote metadata into first-class typed/indexed columns, and backfill existing documents.

**Architecture:** New deterministic helpers do the heavy lifting: `utils/loadfile.py` (format detection + parse), `services/field_mapping.py` (alias + AI column mapping), `services/metadata_normalize.py` (dates/hashes/file-type + record→typed-fields promotion). A new `/ingest/analyze` endpoint returns a proposed mapping; the ingest wizard confirms it; the confirmed `field_mapping` is stored on `IngestJob` and applied by the existing batch builders. An Alembic migration adds the typed columns and backfills.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + Postgres (Neon) backend; Anthropic SDK for AI mapping; React + TypeScript + Vite frontend; pytest (deterministic, `asyncio.run`/`FakeSession`, no DB) for tests.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-16-phase0-sp1-smart-metadata-ingestion-design.md`.
- **Nothing lost:** every promoted column's original raw value stays in `metadata_`; unmapped columns still land in `metadata_`.
- `field_mapping` is stored as `{ <canonical_field>: <source_column_name> }` on a new `IngestJob.field_mapping` JSONB column; all batch handlers apply the same mapping.
- Dates stored as `timestamptz`, normalized to **UTC**; original date string retained in `metadata_`.
- Hashes: **promote** MD5/SHA from the load file if present; **compute** `sha256` from the native file in the **batch handler** (not the analyze step).
- **AI mapping runs only on columns the alias dictionary did not map**, and must **degrade gracefully**: if the Anthropic client is unavailable/errors, those columns stay `unmapped` — ingest is never blocked on AI.
- **Backfill uses the alias dictionary only** (no AI, no network), is idempotent, and never overwrites a value already set.
- Tests are deterministic, no DB and no network, following `backend/tests/test_org_access.py` (`asyncio.run`, `FakeSession`, plain fixtures). Run backend tests from `backend/` with `python -m pytest`. Run frontend checks from `frontend/` with `npm run build`.
- Out of scope (SP2–4): hash dedup logic, email family/threading population, native/PST processing + Tika. Do not build these.

---

## File Structure

**Backend**
- `backend/alembic/versions/<rev>_add_document_metadata_fields.py` *(new)* — schema migration + backfill.
- `backend/app/models.py` *(modify)* — add typed columns to `Document`, `field_mapping` to `IngestJob`.
- `backend/app/utils/loadfile.py` *(new)* — encoding/delimiter detection + `parse_loadfile`.
- `backend/app/services/field_mapping.py` *(new)* — canonical fields, alias dict, `match_aliases`, AI mapping, `build_proposed_mapping`.
- `backend/app/services/metadata_normalize.py` *(new)* — `normalize_date`, `derive_file_type`, `promote_record`.
- `backend/app/services/ingest.py` *(modify)* — apply `field_mapping` in the record→Document builders; compute native sha256.
- `backend/app/routers/ingest.py` *(modify)* — `/ingest/analyze`; `/ingest/process` accepts + persists `field_mapping`.
- `backend/app/schemas.py` *(modify)* — analyze response schema.
- `backend/tests/test_loadfile.py`, `test_field_mapping.py`, `test_metadata_normalize.py` *(new)* + `backend/tests/fixtures/loadfiles/`.

**Frontend**
- `frontend/src/api/client.ts` *(modify)* — `analyzeLoadFile`; `startProcessing` gains `fieldMapping`.
- `frontend/src/components/IngestWizard.tsx` *(modify)* — new `mapping` stage + review table.

---

## Task 1: Schema migration + models

**Files:**
- Create: `backend/alembic/versions/<rev>_add_document_metadata_fields.py`
- Modify: `backend/app/models.py` (`Document` class ~line 90; `IngestJob` class ~line 231)

**Interfaces:**
- Produces: new `Document` attributes `custodian, date_sent, date_received, date_created, date_modified, file_hash_md5, file_hash_sha256, file_type, file_name, source_path, extraction_status, extraction_error, email_from, email_to, email_cc, email_bcc, email_subject`; new `IngestJob.field_mapping`.

- [ ] **Step 1: Add columns to the models**

In `backend/app/models.py`, inside `class Document`, after the existing `is_inclusive` column, add:

```python
    # Phase 0 SP1 — typed metadata (promoted from load-file columns)
    custodian = Column(String(255), nullable=True, index=True)
    date_sent = Column(DateTime(timezone=True), nullable=True, index=True)
    date_received = Column(DateTime(timezone=True), nullable=True)
    date_created = Column(DateTime(timezone=True), nullable=True)
    date_modified = Column(DateTime(timezone=True), nullable=True)
    file_hash_md5 = Column(String(32), nullable=True)
    file_hash_sha256 = Column(String(64), nullable=True, index=True)
    file_type = Column(String(50), nullable=True, index=True)
    file_name = Column(String(500), nullable=True)
    source_path = Column(String(1000), nullable=True)
    extraction_status = Column(String(20), nullable=False, server_default="ok")
    extraction_error = Column(Text, nullable=True)
    email_from = Column(String(500), nullable=True)
    email_to = Column(Text, nullable=True)
    email_cc = Column(Text, nullable=True)
    email_bcc = Column(Text, nullable=True)
    email_subject = Column(String(1000), nullable=True)
```

In `class IngestJob`, after `errors = Column(...)`, add:

```python
    field_mapping = Column(JSONB, nullable=False, default=dict)
```

(`String`, `Text`, `DateTime`, `Column`, `JSONB` are already imported in models.py.)

- [ ] **Step 2: Confirm the current migration head**

Run (from `backend/`): `alembic heads`
Expected: a single head (most recently `k4f9a1b73c80`). Use whatever it prints as `down_revision` in the next step. If `alembic` can't reach a DB in this environment, use `k4f9a1b73c80` (verified latest in `backend/alembic/versions/`) and note it.

- [ ] **Step 3: Write the migration**

Create `backend/alembic/versions/m5a0b2c84d91_add_document_metadata_fields.py`:

```python
"""add typed metadata columns to documents + field_mapping to ingest_jobs

Revision ID: m5a0b2c84d91
Revises: k4f9a1b73c80
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "m5a0b2c84d91"
down_revision = "k4f9a1b73c80"
branch_labels = None
depends_on = None

_DOC_COLUMNS = [
    ("custodian", sa.String(length=255)),
    ("date_sent", sa.DateTime(timezone=True)),
    ("date_received", sa.DateTime(timezone=True)),
    ("date_created", sa.DateTime(timezone=True)),
    ("date_modified", sa.DateTime(timezone=True)),
    ("file_hash_md5", sa.String(length=32)),
    ("file_hash_sha256", sa.String(length=64)),
    ("file_type", sa.String(length=50)),
    ("file_name", sa.String(length=500)),
    ("source_path", sa.String(length=1000)),
    ("extraction_error", sa.Text()),
    ("email_from", sa.String(length=500)),
    ("email_to", sa.Text()),
    ("email_cc", sa.Text()),
    ("email_bcc", sa.Text()),
    ("email_subject", sa.String(length=1000)),
]


def upgrade() -> None:
    for name, type_ in _DOC_COLUMNS:
        op.add_column("documents", sa.Column(name, type_, nullable=True))
    op.add_column(
        "documents",
        sa.Column("extraction_status", sa.String(length=20), nullable=False, server_default="ok"),
    )
    op.add_column(
        "ingest_jobs",
        sa.Column("field_mapping", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_documents_custodian", "documents", ["custodian"])
    op.create_index("ix_documents_date_sent", "documents", ["date_sent"])
    op.create_index("ix_documents_file_hash_sha256", "documents", ["file_hash_sha256"])
    op.create_index("ix_documents_file_type", "documents", ["file_type"])


def downgrade() -> None:
    op.drop_index("ix_documents_file_type", table_name="documents")
    op.drop_index("ix_documents_file_hash_sha256", table_name="documents")
    op.drop_index("ix_documents_date_sent", table_name="documents")
    op.drop_index("ix_documents_custodian", table_name="documents")
    op.drop_column("ingest_jobs", "field_mapping")
    op.drop_column("documents", "extraction_status")
    for name, _ in reversed(_DOC_COLUMNS):
        op.drop_column("documents", name)
```

> The backfill of existing rows is a separate migration (Task 9), written after the promotion helpers exist.

- [ ] **Step 4: Verify models import**

Run (from `backend/`): `python -c "import app.models; print('ok')"` (or `venv/Scripts/python.exe -c ...`)
Expected: `ok`, no error. (If Postgres is reachable, also run `alembic upgrade head` and expect no error; otherwise the migration is exercised against Postgres separately per repo convention.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/m5a0b2c84d91_add_document_metadata_fields.py
git commit -m "feat(ingest): add typed metadata columns + ingest_jobs.field_mapping"
```

---

## Task 2: Smart load-file parser (`utils/loadfile.py`)

**Files:**
- Create: `backend/app/utils/loadfile.py`
- Create: `backend/tests/test_loadfile.py`, `backend/tests/fixtures/loadfiles/`

**Interfaces:**
- Produces:
  - `@dataclass LoadFileParse: encoding: str; delimiter: str; headers: list[str]; sample_rows: list[dict]; total_rows: int`
  - `detect_encoding(raw: bytes) -> str`
  - `detect_delimiter(text_line: str) -> str`
  - `parse_loadfile(path: str, sample_size: int = 20) -> LoadFileParse`

- [ ] **Step 1: Write the failing test + fixtures**

Create fixtures. `backend/tests/fixtures/loadfiles/concordance.dat` (write via Python to get the control bytes exactly — do this in a small script or a conftest; for the test, build content inline). Create `backend/tests/test_loadfile.py`:

```python
"""Unit tests for smart load-file parsing (no DB, no network)."""

import os
import tempfile

from app.utils.loadfile import detect_delimiter, detect_encoding, parse_loadfile

THORN = "þ"   # þ  field wrapper
DC4 = "\x14"       # field separator (Concordance)


def _write(tmp_path, name, text, encoding="utf-8-sig"):
    p = os.path.join(tmp_path, name)
    with open(p, "w", encoding=encoding, newline="") as f:
        f.write(text)
    return p


def test_detect_encoding_bom():
    assert detect_encoding("x".encode("utf-8-sig")) == "utf-8-sig"
    assert detect_encoding("x".encode("utf-16")) in ("utf-16", "utf-16-le", "utf-16-be")
    assert detect_encoding("plain ascii".encode("utf-8")) == "utf-8"


def test_detect_delimiter():
    assert detect_delimiter(f"{THORN}A{THORN}{DC4}{THORN}B{THORN}") == DC4
    assert detect_delimiter("A,B,C") == ","
    assert detect_delimiter("A\tB\tC") == "\t"
    assert detect_delimiter("A|B|C") == "|"


def test_parse_concordance_dat():
    with tempfile.TemporaryDirectory() as tmp:
        header = DC4.join(f"{THORN}{h}{THORN}" for h in ["Begin Bates", "Custodian"])
        row = DC4.join(f"{THORN}{v}{THORN}" for v in ["ABC-1", "Smith, J"])
        path = _write(tmp, "load.dat", f"{header}\r\n{row}\r\n")
        parsed = parse_loadfile(path)
        assert parsed.headers == ["Begin Bates", "Custodian"]
        assert parsed.total_rows == 1
        assert parsed.sample_rows[0] == {"Begin Bates": "ABC-1", "Custodian": "Smith, J"}


def test_parse_csv():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "load.csv", "BegBates,Custodian\r\nABC-1,Jones\r\n", encoding="utf-8")
        parsed = parse_loadfile(path)
        assert parsed.headers == ["BegBates", "Custodian"]
        assert parsed.sample_rows[0]["Custodian"] == "Jones"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_loadfile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.utils.loadfile'`.

- [ ] **Step 3: Implement `utils/loadfile.py`**

```python
"""Smart load-file parsing: detect encoding + delimiter and parse to records.

Tolerates Concordance (þ-wrapped, DC4-separated), CSV, TAB, and pipe formats,
with UTF-8/UTF-16/Windows-1252 encodings, so ingest is not tied to one layout.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

THORN = "þ"        # þ  Concordance field wrapper / quote
DC4 = "\x14"            # Concordance field separator
_DELIMS = [DC4, "\t", ",", "|"]


@dataclass
class LoadFileParse:
    encoding: str
    delimiter: str
    headers: list[str]
    sample_rows: list[dict]
    total_rows: int


def detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def detect_delimiter(text_line: str) -> str:
    # Concordance DC4 wins if present; else the most frequent candidate.
    if DC4 in text_line:
        return DC4
    best, best_count = ",", 0
    for d in ["\t", ",", "|"]:
        c = text_line.count(d)
        if c > best_count:
            best, best_count = d, c
    return best


def _split(row: str, delimiter: str) -> list[str]:
    if delimiter == DC4:
        return [f.strip(THORN).strip() for f in row.split(DC4)]
    reader = csv.reader(io.StringIO(row), delimiter=delimiter)
    fields = next(reader, [])
    return [f.strip() for f in fields]


def parse_loadfile(path: str, sample_size: int = 20) -> LoadFileParse:
    with open(path, "rb") as f:
        raw = f.read()
    encoding = detect_encoding(raw)
    text = raw.decode(encoding, errors="replace").replace("\x00", "")

    lines = text.strip().split("\r\n")
    if len(lines) == 1 and "\n" in lines[0]:
        lines = text.strip().split("\n")
    if not lines or not lines[0].strip():
        return LoadFileParse(encoding, ",", [], [], 0)

    delimiter = detect_delimiter(lines[0])
    headers = _split(lines[0], delimiter)

    rows: list[dict] = []
    total = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        total += 1
        if len(rows) < sample_size:
            values = _split(line, delimiter)
            record = {}
            for i, h in enumerate(headers):
                v = values[i] if i < len(values) else ""
                if v and "\\" in v:
                    v = v.replace("\\", "/")
                record[h] = v
            rows.append(record)
    return LoadFileParse(encoding, delimiter, headers, rows, total)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_loadfile.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/utils/loadfile.py backend/tests/test_loadfile.py backend/tests/fixtures
git commit -m "feat(ingest): smart load-file encoding + delimiter detection and parse"
```

---

## Task 3: Alias dictionary + `match_aliases` (`services/field_mapping.py`)

**Files:**
- Create: `backend/app/services/field_mapping.py`
- Create: `backend/tests/test_field_mapping.py`

**Interfaces:**
- Produces:
  - `CANONICAL_FIELDS: list[str]` — valid mapping targets.
  - `ALIAS_DICT: dict[str, list[str]]` — canonical → known header variants.
  - `match_aliases(headers: list[str]) -> dict[str, str]` — `{canonical: source_header}` for headers the dictionary recognizes.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_field_mapping.py`:

```python
"""Unit tests for alias-based column mapping (no DB, no network)."""

from app.services import field_mapping as fm


def test_canonical_fields_include_metadata_and_structural():
    for f in ["bates_begin", "custodian", "date_sent", "file_hash_md5",
              "email_to", "file_name", "source_path"]:
        assert f in fm.CANONICAL_FIELDS


def test_match_aliases_is_insensitive_to_case_space_underscore():
    headers = ["BEGDOC", "Cust", "Date Sent", "MD5 Hash", "Email_To", "FileName"]
    m = fm.match_aliases(headers)
    assert m["bates_begin"] == "BEGDOC"
    assert m["custodian"] == "Cust"
    assert m["date_sent"] == "Date Sent"
    assert m["file_hash_md5"] == "MD5 Hash"
    assert m["email_to"] == "Email_To"
    assert m["file_name"] == "FileName"


def test_match_aliases_ignores_unknown_headers():
    m = fm.match_aliases(["Wingding", "Custodian"])
    assert m == {"custodian": "Custodian"}


def test_match_aliases_first_wins_on_duplicate_target():
    m = fm.match_aliases(["Begin Bates", "BEGDOC"])
    assert m["bates_begin"] == "Begin Bates"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_field_mapping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.field_mapping'`.

- [ ] **Step 3: Implement the alias layer**

```python
"""Map load-file columns to canonical fields: a deterministic alias dictionary
plus (Task 4) an AI-assisted fallback for unrecognized columns."""

from __future__ import annotations

# Canonical fields that a source column can map to. "text_link"/"native_link"
# are structural (used to locate text/native files); the rest are typed columns.
CANONICAL_FIELDS: list[str] = [
    "bates_begin", "bates_end", "page_count", "text_link", "native_link",
    "custodian", "date_sent", "date_received", "date_created", "date_modified",
    "file_hash_md5", "file_hash_sha256", "file_type", "file_name", "source_path",
    "email_from", "email_to", "email_cc", "email_bcc", "email_subject",
]

ALIAS_DICT: dict[str, list[str]] = {
    "bates_begin": ["Begin Bates", "BegBates", "BEGDOC", "Bates Beg", "Bates Begin", "DocID", "Production::Begin Bates"],
    "bates_end": ["End Bates", "EndBates", "ENDDOC", "Bates End", "Production::End Bates"],
    "page_count": ["Page Count", "Pages", "PageCount", "Num Pages"],
    "text_link": ["Text Link", "Extracted Text", "OCR Path", "TextLink", "Text Path"],
    "native_link": ["Native Link", "NativeLink", "Native Path", "File Path", "Native File"],
    "custodian": ["Custodian", "Cust", "Custodian Name", "Source Custodian"],
    "date_sent": ["Date Sent", "Sent", "DateSent", "Sent Date", "Email Sent Date"],
    "date_received": ["Date Received", "Received", "DateReceived", "Received Date"],
    "date_created": ["Date Created", "Created", "DateCreated", "Creation Date", "File Created"],
    "date_modified": ["Date Modified", "Modified", "DateModified", "Last Modified", "File Modified"],
    "file_hash_md5": ["MD5", "MD5 Hash", "Hash", "MD5Hash", "MD5 Digest"],
    "file_hash_sha256": ["SHA256", "SHA-256", "SHA256 Hash", "SHA Hash"],
    "file_type": ["File Type", "FileType", "Type", "File Extension", "Extension", "Doc Type"],
    "file_name": ["File Name", "FileName", "Filename", "Original File Name", "Document Name"],
    "source_path": ["Source Path", "SourcePath", "Original Path", "Full Path", "Path"],
    "email_from": ["From", "Email From", "Author", "Sender"],
    "email_to": ["To", "Email To", "Recipients", "Recipient"],
    "email_cc": ["CC", "Email CC", "Copyee"],
    "email_bcc": ["BCC", "Email BCC", "Blind Copyee"],
    "email_subject": ["Subject", "Email Subject", "Title"],
}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# Precompute normalized alias → canonical (first canonical wins per alias).
_NORM_ALIAS: dict[str, str] = {}
for _canon, _aliases in ALIAS_DICT.items():
    for _a in _aliases:
        _NORM_ALIAS.setdefault(_norm(_a), _canon)


def match_aliases(headers: list[str]) -> dict[str, str]:
    """Return {canonical_field: source_header} for recognized headers.
    First header wins if two map to the same canonical field."""
    result: dict[str, str] = {}
    for h in headers:
        canon = _NORM_ALIAS.get(_norm(h))
        if canon and canon not in result:
            result[canon] = h
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_field_mapping.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/field_mapping.py backend/tests/test_field_mapping.py
git commit -m "feat(ingest): alias dictionary and match_aliases column mapper"
```

---

## Task 4: AI-assisted mapping + `build_proposed_mapping`

**Files:**
- Modify: `backend/app/services/field_mapping.py`
- Modify: `backend/tests/test_field_mapping.py`

**Interfaces:**
- Consumes: `match_aliases`, `CANONICAL_FIELDS`.
- Produces:
  - `propose_ai_mapping(columns: list[dict], client=None) -> dict[str, str]` — `{source_name: canonical}` for provided unmapped columns; `{}` on any failure/unavailable client. Each input column is `{"name": str, "samples": list[str]}`.
  - `build_proposed_mapping(headers: list[str], sample_rows: list[dict], use_ai: bool = True) -> list[dict]` — one entry per header: `{"source_name", "samples", "target", "confidence", "source"}` where `source ∈ {"alias","ai","unmapped"}`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_field_mapping.py`:

```python
def test_build_proposed_mapping_alias_and_unmapped_without_ai():
    headers = ["Custodian", "Widget Code"]
    rows = [{"Custodian": "Smith", "Widget Code": "X1"}]
    proposed = fm.build_proposed_mapping(headers, rows, use_ai=False)
    by_name = {p["source_name"]: p for p in proposed}
    assert by_name["Custodian"]["target"] == "custodian"
    assert by_name["Custodian"]["source"] == "alias"
    assert by_name["Custodian"]["confidence"] == 1.0
    assert by_name["Widget Code"]["target"] is None
    assert by_name["Widget Code"]["source"] == "unmapped"
    assert by_name["Custodian"]["samples"] == ["Smith"]


def test_propose_ai_mapping_uses_client(monkeypatch):
    captured = {}

    class _FakeContent:
        def __init__(self, data):
            self.text = data

    class _FakeMsg:
        def __init__(self, data):
            self.content = [_FakeContent(data)]

    class _FakeMessages:
        def create(self, **kwargs):
            captured["prompt"] = kwargs
            import json
            return _FakeMsg(json.dumps({"Widget Code": "file_type"}))

    class _FakeClient:
        messages = _FakeMessages()

    out = fm.propose_ai_mapping([{"name": "Widget Code", "samples": ["X1"]}], client=_FakeClient())
    assert out == {"Widget Code": "file_type"}


def test_propose_ai_mapping_falls_back_to_empty_on_error(monkeypatch):
    class _BoomClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("no api key")
    assert fm.propose_ai_mapping([{"name": "X", "samples": ["1"]}], client=_BoomClient()) == {}
    # No client available at all -> empty, never raises.
    assert fm.propose_ai_mapping([{"name": "X", "samples": ["1"]}], client=None) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_field_mapping.py -v`
Expected: FAIL — `AttributeError: module 'app.services.field_mapping' has no attribute 'build_proposed_mapping'`.

- [ ] **Step 3: Implement the AI layer**

Append to `backend/app/services/field_mapping.py`:

```python
import json
import logging

logger = logging.getLogger(__name__)

_AI_MODEL = "claude-opus-4-8"
_MAX_SAMPLES = 3


def _default_client():
    """Return an Anthropic client if a key is configured, else None."""
    from app.config import settings
    if not settings.anthropic_api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def propose_ai_mapping(columns: list[dict], client=None) -> dict[str, str]:
    """Ask the model to map unrecognized columns to canonical fields.
    Returns {source_name: canonical_field}. Never raises — returns {} on any
    failure or when no client/key is available (ingest must not block on AI)."""
    if not columns:
        return {}
    if client is None:
        client = _default_client()
    if client is None:
        return {}

    lines = []
    for c in columns:
        samples = ", ".join(str(s) for s in c.get("samples", [])[:_MAX_SAMPLES] if s)
        lines.append(f'- "{c["name"]}" (examples: {samples})')
    prompt = (
        "Map each e-discovery load-file column to ONE canonical field, or null if none fits.\n"
        f"Canonical fields: {', '.join(CANONICAL_FIELDS)}\n\n"
        "Columns:\n" + "\n".join(lines) + "\n\n"
        'Respond with ONLY a JSON object {"<column name>": "<canonical field or null>"}.'
    )
    try:
        msg = client.messages.create(
            model=_AI_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        parsed = json.loads(raw)
    except Exception:
        logger.warning("AI column mapping failed; leaving columns unmapped", exc_info=True)
        return {}

    valid = set(CANONICAL_FIELDS)
    return {
        name: target for name, target in parsed.items()
        if isinstance(target, str) and target in valid
    }


def build_proposed_mapping(headers: list[str], sample_rows: list[dict], use_ai: bool = True) -> list[dict]:
    """Combine alias matching + AI fallback into a per-column proposal list."""
    alias_map = match_aliases(headers)                     # canonical -> source
    source_to_canon = {src: canon for canon, src in alias_map.items()}

    def samples_for(name: str) -> list[str]:
        vals = []
        for r in sample_rows:
            v = r.get(name)
            if v:
                vals.append(v)
            if len(vals) >= _MAX_SAMPLES:
                break
        return vals

    unmapped = [h for h in headers if h not in source_to_canon]
    ai_map: dict[str, str] = {}
    if use_ai and unmapped:
        ai_map = propose_ai_mapping(
            [{"name": h, "samples": samples_for(h)} for h in unmapped]
        )

    proposed = []
    for h in headers:
        if h in source_to_canon:
            proposed.append({"source_name": h, "samples": samples_for(h),
                             "target": source_to_canon[h], "confidence": 1.0, "source": "alias"})
        elif h in ai_map:
            proposed.append({"source_name": h, "samples": samples_for(h),
                             "target": ai_map[h], "confidence": 0.6, "source": "ai"})
        else:
            proposed.append({"source_name": h, "samples": samples_for(h),
                             "target": None, "confidence": 0.0, "source": "unmapped"})
    return proposed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_field_mapping.py -v`
Expected: PASS (all Task 3 + Task 4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/field_mapping.py backend/tests/test_field_mapping.py
git commit -m "feat(ingest): AI-assisted column mapping with graceful fallback"
```

---

## Task 5: Normalization + `promote_record` (`services/metadata_normalize.py`)

**Files:**
- Create: `backend/app/services/metadata_normalize.py`
- Create: `backend/tests/test_metadata_normalize.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure).
- Produces:
  - `normalize_date(value: str) -> datetime | None` — tz-aware UTC datetime or None.
  - `derive_file_type(name_or_path: str | None) -> str | None` — lowercase extension without dot.
  - `promote_record(record: dict, field_mapping: dict[str, str]) -> tuple[dict, dict]` — `(typed_fields, leftover_metadata)`. `typed_fields` keys are canonical metadata fields with normalized values; `leftover_metadata` is every source column not consumed by a structural/metadata target (originals preserved).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_metadata_normalize.py`:

```python
"""Unit tests for metadata normalization + promotion (no DB, no network)."""

from datetime import timezone

from app.services.metadata_normalize import derive_file_type, normalize_date, promote_record


def test_normalize_date_iso_and_us_and_ampm():
    assert normalize_date("2026-07-16T13:45:00Z").tzinfo is not None
    d = normalize_date("07/16/2026 01:45 PM")
    assert (d.year, d.month, d.day, d.hour) == (2026, 7, 16, 13)
    assert normalize_date("07/16/2026").year == 2026
    assert normalize_date("") is None
    assert normalize_date("not a date") is None
    # stored UTC
    assert normalize_date("2026-07-16T13:45:00Z").astimezone(timezone.utc).hour == 13


def test_derive_file_type():
    assert derive_file_type("C:/x/report.PDF") == "pdf"
    assert derive_file_type("mail.msg") == "msg"
    assert derive_file_type(None) is None
    assert derive_file_type("noext") is None


def test_promote_record_maps_typed_fields_and_keeps_leftovers():
    record = {"Cust": "Smith, J", "Sent": "07/16/2026", "Widget": "keepme", "MD5": "abc123"}
    mapping = {"custodian": "Cust", "date_sent": "Sent", "file_hash_md5": "MD5"}
    typed, leftover = promote_record(record, mapping)
    assert typed["custodian"] == "Smith, J"
    assert typed["date_sent"].year == 2026
    assert typed["file_hash_md5"] == "abc123"
    # unmapped column preserved; mapped originals still kept in leftover metadata
    assert leftover["Widget"] == "keepme"
    assert leftover["Sent"] == "07/16/2026"   # original string retained (nothing lost)


def test_promote_record_ignores_structural_targets():
    # bates/text_link/native_link are structural — not returned as typed metadata
    typed, _ = promote_record({"BegBates": "ABC-1"}, {"bates_begin": "BegBates"})
    assert "bates_begin" not in typed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metadata_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.metadata_normalize'`.

- [ ] **Step 3: Implement**

```python
"""Normalize load-file values and promote them to typed Document fields."""

from __future__ import annotations

import os
from datetime import datetime, timezone

# Typed metadata fields promote_record emits (structural fields excluded).
_METADATA_TARGETS = {
    "custodian", "date_sent", "date_received", "date_created", "date_modified",
    "file_hash_md5", "file_hash_sha256", "file_type", "file_name", "source_path",
    "email_from", "email_to", "email_cc", "email_bcc", "email_subject",
}
_STRUCTURAL_TARGETS = {"bates_begin", "bates_end", "page_count", "text_link", "native_link"}
_DATE_TARGETS = {"date_sent", "date_received", "date_created", "date_modified"}

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M", "%m/%d/%Y",
]


def normalize_date(value: str) -> datetime | None:
    if not value or not value.strip():
        return None
    v = value.strip().replace("Z", "+0000")
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(v, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def derive_file_type(name_or_path: str | None) -> str | None:
    if not name_or_path:
        return None
    ext = os.path.splitext(name_or_path)[1].lstrip(".").lower()
    return ext or None


def promote_record(record: dict, field_mapping: dict[str, str]) -> tuple[dict, dict]:
    """Return (typed_fields, leftover_metadata).

    typed_fields: canonical metadata field -> normalized value (dates parsed).
    leftover_metadata: ALL non-empty source columns EXCEPT those consumed by a
    structural target — mapped-metadata originals are still kept here so nothing
    is lost.
    """
    typed: dict = {}
    for canon, source_col in field_mapping.items():
        if canon not in _METADATA_TARGETS:
            continue
        raw = (record.get(source_col) or "").strip()
        if not raw:
            continue
        if canon in _DATE_TARGETS:
            dt = normalize_date(raw)
            if dt is not None:
                typed[canon] = dt
        elif canon == "file_type":
            typed[canon] = raw.lstrip(".").lower()[:50]
        else:
            typed[canon] = raw

    structural_cols = {
        source_col for canon, source_col in field_mapping.items()
        if canon in _STRUCTURAL_TARGETS
    }
    leftover = {
        k: v for k, v in record.items()
        if v and k not in structural_cols
    }
    return typed, leftover
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_metadata_normalize.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/metadata_normalize.py backend/tests/test_metadata_normalize.py
git commit -m "feat(ingest): date/file-type normalization and record promotion"
```

---

## Task 6: Apply the mapping in the record→Document builders

**Files:**
- Modify: `backend/app/services/ingest.py` (the two `Document(...)` builders at ~line 113 and ~line 292; thread `field_mapping` from the job into `run_ingest_batch`/`bootstrap_ingest_source`)

**Interfaces:**
- Consumes: `promote_record` (Task 5), `match_aliases`/`derive_file_type` (Tasks 3/5), the job's `field_mapping`.
- Produces: Documents populated with typed metadata fields.

- [ ] **Step 1: Add a promotion helper + use it in the storage-path builder**

At the top of `ingest.py`, add imports:

```python
from app.services.field_mapping import match_aliases
from app.services.metadata_normalize import derive_file_type, promote_record
```

Add a helper near the builders:

```python
def _effective_mapping(record_keys, field_mapping: dict | None) -> dict:
    """Use the confirmed field_mapping if present; else fall back to alias
    matching over the record's own columns (keeps ingest working without an
    explicit mapping, e.g. the legacy/inline path)."""
    if field_mapping:
        return field_mapping
    return match_aliases(list(record_keys))


def _apply_metadata(doc, record: dict, field_mapping: dict | None) -> None:
    """Promote typed metadata onto a freshly built Document."""
    mapping = _effective_mapping(record.keys(), field_mapping)
    typed, leftover = promote_record(record, mapping)
    for field, value in typed.items():
        setattr(doc, field, value)
    if not doc.file_type:
        doc.file_type = derive_file_type(doc.file_name or doc.native_path)
    # Preserve original values for everything not structural.
    doc.metadata_ = leftover
```

In the storage-path builder (the function that `return Document(...)` at ~line 292), change it to build the Document, then apply metadata before returning. Replace:

```python
    metadata = {}
    for key, value in record.items():
        if key not in FIELD_MAP and value:
            metadata[key] = value

    return Document(
        production_id=production_id,
        bates_begin=bates_begin,
        bates_end=bates_end,
        page_count=page_count,
        metadata_=metadata,
        text_content=text_content,
        native_path=native_storage_path,
        image_paths=jpeg_storage_paths,
    )
```

with:

```python
    doc = Document(
        production_id=production_id,
        bates_begin=bates_begin,
        bates_end=bates_end,
        page_count=page_count,
        metadata_={},
        text_content=text_content,
        native_path=native_storage_path,
        image_paths=jpeg_storage_paths,
        file_name=record.get(FIELD_MAP_REVERSED.get("native_link", "Native Link"), "") or None,
    )
    _apply_metadata(doc, record, field_mapping)
    return doc
```

Add `field_mapping: dict | None = None` as a parameter to this builder function's signature and thread it through from `run_ingest_batch`. (Do the same minimal change to the inline disk builder at ~line 113 if it is reachable in tests; otherwise leave a `# TODO(SP1): inline path shares _apply_metadata` — NO: instead apply the same `_apply_metadata(doc, record, None)` call there so both paths behave identically.)

> `FIELD_MAP_REVERSED` — add near `FIELD_MAP`: `FIELD_MAP_REVERSED = {v: k for k, v in FIELD_MAP.items()}`. (Used only to find the original file-name-ish column; if absent the value is simply None.)

- [ ] **Step 2: Thread `field_mapping` from the job into the batch**

In `run_ingest_batch` (~line 564), load the job's `field_mapping` and pass it to the builder. After fetching the job/records, read `job.field_mapping` (default `{}`) and pass it into each builder call.

- [ ] **Step 3: Verify import + existing tests still pass**

Run (from `backend/`): `python -c "import app.services.ingest; print('ok')"` then `python -m pytest -q`
Expected: `ok`; suite passes except the known pre-existing unrelated failure `test_ai_review.py::test_build_classification_prompt`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/ingest.py
git commit -m "feat(ingest): promote typed metadata onto documents via field_mapping"
```

---

## Task 7: `/ingest/analyze` endpoint + `field_mapping` on process

**Files:**
- Modify: `backend/app/routers/ingest.py`, `backend/app/schemas.py`, `backend/app/services/ingest.py`

**Interfaces:**
- Consumes: `parse_loadfile` (Task 2), `build_proposed_mapping` (Task 4), `bootstrap_ingest_source` / storage helpers.
- Produces: `POST /api/ingest/analyze {production_id} -> {format, delimiter, columns: [...], sample_rows: [...]}`; `POST /api/ingest/process` now accepts `field_mapping` and stores it on the `IngestJob`.

- [ ] **Step 1: Add an analyze service helper**

In `backend/app/services/ingest.py`, add a function that locates + downloads the load file for a production (reuse whatever `bootstrap_ingest_source` uses to fetch the DAT to a temp path) and returns a `build_proposed_mapping` result:

```python
def analyze_load_file(production_id: int) -> dict:
    """Parse the uploaded load file and propose a column mapping."""
    from app.utils.loadfile import parse_loadfile
    from app.services.field_mapping import build_proposed_mapping

    dat_path = _download_dat_to_temp(production_id)   # reuse existing DAT-fetch logic
    parsed = parse_loadfile(dat_path)
    columns = build_proposed_mapping(parsed.headers, parsed.sample_rows)
    return {
        "format": "concordance" if parsed.delimiter == "\x14" else "delimited",
        "delimiter": parsed.delimiter,
        "columns": columns,
        "sample_rows": parsed.sample_rows,
        "total_rows": parsed.total_rows,
    }
```

If `bootstrap_ingest_source` already downloads the DAT internally, extract that download into `_download_dat_to_temp(production_id) -> str` and call it from both.

- [ ] **Step 2: Add the endpoint**

In `backend/app/routers/ingest.py`, add:

```python
@router.post("/ingest/analyze")
async def analyze_ingest(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Parse the uploaded load file and return a proposed column mapping."""
    from app.services.ingest import analyze_load_file

    production_id = body.get("production_id")
    if not production_id:
        raise HTTPException(status_code=400, detail="production_id is required")
    production = await db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=404, detail="Production not found")
    if production.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    try:
        return await run_in_threadpool(analyze_load_file, int(production_id))
    except Exception as e:
        logger.exception("Analyze failed")
        raise HTTPException(status_code=400, detail=f"Could not analyze load file: {e}")
```

Add `from starlette.concurrency import run_in_threadpool` to the imports (the parse is sync/blocking).

- [ ] **Step 3: Persist `field_mapping` in `/ingest/process`**

In `start_processing`, after reading the body, add:

```python
    field_mapping = body.get("field_mapping") or {}
```

and set `field_mapping=field_mapping` on **both** `IngestJob(...)` constructions in that function.

- [ ] **Step 4: Verify**

Run (from `backend/`): `python -c "import app.routers.ingest; print('ok')"` then `python -m pytest -q`
Expected: `ok`; suite green (minus the known pre-existing unrelated failure).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ingest.py backend/app/services/ingest.py backend/app/schemas.py
git commit -m "feat(ingest): /ingest/analyze proposed mapping + persist field_mapping on job"
```

---

## Task 8: Compute SHA-256 from the native file

**Files:**
- Modify: `backend/app/services/ingest.py` (storage-path builder — where `native_storage_path` is known)

**Interfaces:**
- Consumes: `get_download_bytes` (already used in ingest.py), the built Document.
- Produces: `Document.file_hash_sha256` populated from native bytes; `extraction_status`/`extraction_error` on failure.

- [ ] **Step 1: Add hashing in the builder**

In the storage-path builder, after `_apply_metadata(...)` and before `return doc`, add:

```python
    if native_storage_path and not doc.file_hash_sha256:
        try:
            import hashlib
            from app.services.storage import get_download_bytes
            native_bytes = get_download_bytes(native_storage_path)
            doc.file_hash_sha256 = hashlib.sha256(native_bytes).hexdigest()
        except Exception as e:
            doc.extraction_status = "partial"
            doc.extraction_error = f"sha256 from native failed: {e}"
            errors.append(f"{bates_begin}: sha256 from native failed: {e}")
```

(`errors` is the batch-local error list already in scope in that builder.)

- [ ] **Step 2: Verify**

Run (from `backend/`): `python -c "import app.services.ingest; print('ok')"` then `python -m pytest -q`
Expected: `ok`; suite green (minus the known pre-existing failure). No new test needed — this path requires Storage I/O and is covered by manual/integration verification; the promotion logic it depends on is unit-tested in Task 5.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/ingest.py
git commit -m "feat(ingest): compute sha256 from native file during batch build"
```

---

## Task 9: Backfill existing documents

**Files:**
- Create: `backend/alembic/versions/n6b1c3d95e02_backfill_document_metadata.py`
- Create: `backend/tests/test_backfill_metadata.py`

**Interfaces:**
- Consumes: `match_aliases`, `promote_record`.
- Produces: a reusable pure function `backfill_typed_fields(metadata: dict) -> dict` (typed fields derivable from an existing `metadata_` dict, alias-only) + a data migration that applies it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_backfill_metadata.py`:

```python
"""Unit tests for alias-only backfill derivation (no DB, no network)."""

from app.services.metadata_normalize import backfill_typed_fields


def test_backfill_derives_typed_fields_from_metadata():
    meta = {"Custodian": "Doe, J", "Date Sent": "03/04/2025", "Widget": "x"}
    typed = backfill_typed_fields(meta)
    assert typed["custodian"] == "Doe, J"
    assert typed["date_sent"].year == 2025
    assert "Widget" not in typed


def test_backfill_empty_when_nothing_recognized():
    assert backfill_typed_fields({"Widget": "x"}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backfill_metadata.py -v`
Expected: FAIL — `ImportError: cannot import name 'backfill_typed_fields'`.

- [ ] **Step 3: Implement `backfill_typed_fields` (in `metadata_normalize.py`)**

```python
def backfill_typed_fields(metadata: dict) -> dict:
    """Derive typed metadata fields from an existing metadata_ dict using the
    alias dictionary only (deterministic; no AI)."""
    from app.services.field_mapping import match_aliases
    mapping = match_aliases(list(metadata.keys()))
    typed, _ = promote_record(metadata, mapping)
    return typed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backfill_metadata.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Write the data migration**

Create `backend/alembic/versions/n6b1c3d95e02_backfill_document_metadata.py`:

```python
"""backfill typed metadata on existing documents from metadata_ (alias-only)

Revision ID: n6b1c3d95e02
Revises: m5a0b2c84d91
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "n6b1c3d95e02"
down_revision = "m5a0b2c84d91"
branch_labels = None
depends_on = None

_SET_COLUMNS = [
    "custodian", "date_sent", "date_received", "date_created", "date_modified",
    "file_hash_md5", "file_hash_sha256", "file_type", "file_name", "source_path",
    "email_from", "email_to", "email_cc", "email_bcc", "email_subject",
]


def upgrade() -> None:
    from app.services.metadata_normalize import backfill_typed_fields
    conn = op.get_bind()
    rows = conn.execute(sa.text('SELECT id, metadata FROM documents')).fetchall()
    for row in rows:
        meta = row.metadata or {}
        typed = backfill_typed_fields(meta)
        if not typed:
            continue
        # Only set columns currently NULL (idempotent; never overwrite).
        sets, params = [], {"id": row.id}
        for col in _SET_COLUMNS:
            if col in typed:
                sets.append(f"{col} = COALESCE({col}, :{col})")
                params[col] = typed[col]
        if sets:
            conn.execute(sa.text(f"UPDATE documents SET {', '.join(sets)} WHERE id = :id"), params)


def downgrade() -> None:
    pass  # data backfill; no structural change to reverse
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/metadata_normalize.py backend/tests/test_backfill_metadata.py backend/alembic/versions/n6b1c3d95e02_backfill_document_metadata.py
git commit -m "feat(ingest): alias-only backfill of typed metadata for existing docs"
```

---

## Task 10: Ingest wizard mapping-review UI

**Files:**
- Modify: `frontend/src/api/client.ts`, `frontend/src/components/IngestWizard.tsx`

**Interfaces:**
- Consumes: `POST /api/ingest/analyze`; `startProcessing` with `field_mapping`.
- Produces: a `mapping` wizard stage; `analyzeLoadFile(productionId)`.

- [ ] **Step 1: Add the client functions**

In `frontend/src/api/client.ts`, add near `startProcessing`:

```typescript
export interface ProposedColumn {
  source_name: string;
  samples: string[];
  target: string | null;
  confidence: number;
  source: 'alias' | 'ai' | 'unmapped';
}

export const analyzeLoadFile = (productionId: number) =>
  request<{ format: string; delimiter: string; columns: ProposedColumn[]; sample_rows: Record<string, string>[]; total_rows: number }>(
    '/api/ingest/analyze', json({ production_id: productionId }),
  );
```

Change `startProcessing` to accept and send the mapping:

```typescript
export const startProcessing = (
  productionId: number,
  totalFiles: number,
  sourceFormat: 'relativity' | 'generic_pdf' = 'relativity',
  fieldMapping: Record<string, string> = {},
) =>
  request<IngestJob>('/api/ingest/process', json({ production_id: productionId, total_files: totalFiles, source_format: sourceFormat, field_mapping: fieldMapping }));
```

- [ ] **Step 2: Add the `mapping` stage to the wizard**

In `frontend/src/components/IngestWizard.tsx`:
- Extend the stage type: `type Stage = 'setup' | 'uploading' | 'mapping' | 'processing' | 'complete' | 'error';`
- Import `analyzeLoadFile`, `type ProposedColumn`.
- Add state: `const [columns, setColumns] = useState<ProposedColumn[]>([]); const [mappingProdId, setMappingProdId] = useState<number | null>(null);`
- After the upload loop completes, **for `relativity` mode only**, instead of calling `startProcessing` immediately: set `mappingProdId`, call `analyzeLoadFile(production_id)`, `setColumns(res.columns)`, `setStage('mapping')`. (For `generic_pdf`, keep the existing direct `startProcessing` path — no load file to map.)
- Render the `mapping` stage: a table with one row per `columns[i]` showing `source_name`, `samples.join(', ')`, a `<select>` of canonical targets (value = `target ?? ''`, options: the canonical field list + a "— leave in metadata —" empty option), and a badge for `source` (alias=green, ai=amber, unmapped=grey). Editing a row updates `columns[i].target`.
- A "Start processing" button builds `field_mapping` from the confirmed columns: `Object.fromEntries(columns.filter(c => c.target).map(c => [c.target!, c.source_name]))`, then `setStage('processing')` and `startProcessing(mappingProdId!, totalFiles, 'relativity', fieldMapping)` and resumes the existing status-polling.

The canonical target options (must match backend `CANONICAL_FIELDS`):
```
bates_begin, bates_end, page_count, text_link, native_link, custodian,
date_sent, date_received, date_created, date_modified, file_hash_md5,
file_hash_sha256, file_type, file_name, source_path, email_from, email_to,
email_cc, email_bcc, email_subject
```

- [ ] **Step 3: Build**

Run (from `frontend/`): `npm run build`
Expected: succeeds, no type errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/components/IngestWizard.tsx
git commit -m "feat(ingest): wizard column-mapping review step with AI proposals"
```

---

## Self-Review

**Spec coverage:**
- Typed schema + migration + `IngestJob.field_mapping` → Task 1. ✓
- Smart parsing (encoding/delimiter/quote) → Task 2. ✓
- Alias dictionary → Task 3; AI-assisted mapping (only unmapped, graceful fallback) + `ProposedMapping` → Task 4. ✓
- Normalization (dates UTC / file_type / promotion, originals retained) → Task 5. ✓
- Apply mapping when building Documents → Task 6. ✓
- analyze→confirm→process flow + persist mapping → Task 7; wizard UI → Task 10. ✓
- sha256 from native in the batch handler → Task 8. ✓
- Backfill (alias-only, idempotent) → Task 9. ✓
- Error handling (`extraction_status`/`extraction_error`, surfaced in job errors) → Tasks 6/8. ✓
- Tests deterministic/no-DB → Tasks 2–5, 9. AI path mocked → Task 4. Native sha256 + endpoints are integration-verified (no DB in unit suite) → Tasks 7/8 (noted). ✓
- Out of scope (dedup/families/native-processing) — not implemented. ✓

**Placeholder scan:** One deliberate reuse note in Task 7 (`_download_dat_to_temp` — "reuse existing DAT-fetch logic") and Task 6 (`FIELD_MAP_REVERSED`) — both specify exactly what to do with concrete code; no "TBD"/"add error handling". ✓

**Type consistency:**
- `field_mapping` is `{canonical: source_column}` in the migration, `promote_record`, backfill, endpoint, and the wizard's `Object.fromEntries([target, source_name])` — consistent.
- `build_proposed_mapping` entry keys (`source_name/samples/target/confidence/source`) match the frontend `ProposedColumn` interface and the wizard consumption — consistent.
- `promote_record(record, field_mapping) -> (typed, leftover)` signature identical across Tasks 5, 6, 9. ✓
- `match_aliases -> {canonical: source}` consistent across Tasks 3, 4, 6, 9. ✓

**Assumptions to verify during implementation:**
- Current migration head (Task 1 Step 2 — confirm via `alembic heads`; likely `k4f9a1b73c80`).
- The exact DAT-download helper inside `bootstrap_ingest_source` to factor into `_download_dat_to_temp` (Task 7).
- The storage-path builder's local variable names (`native_storage_path`, `errors`, `bates_begin`) as read at ingest.py:283–301.
