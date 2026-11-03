# Large-Mailbox Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load multi-GB PST/OST files completely: stream them from storage, let the uploader pick item types, process in parallel chunks, prove completeness, and hold AI analysis until asked.

**Architecture:** A large container goes through a different path from the rest of the load:
1. A *survey* task reads the PST's folder tree with pypff over a seekable Storage stream and records counts and sizes per item type.
2. The uploader picks types on a new wizard screen, which writes `ingest_chunks` rows and enqueues one Cloud Task per chunk.
3. Chunk tasks stream their slice, build Documents with the existing `build_email_documents`, and commit per message.
4. Completion is derived from chunk rows (`kind` = items → embed → post) with an atomic finalize.
5. Large loads set a sticky `ai_hold` on the production.
6. Per-document AI stages move to `claude-haiku-5-5`, using the Message Batches API for big runs.

**Tech Stack:** FastAPI, SQLAlchemy async + asyncpg, Alembic, Cloud Tasks, Cloud Run (`vigilist-worker`), `libpff-python` (pypff), PyMuPDF, Anthropic Python SDK, React + TypeScript (Vite).

**Spec:** `docs/superpowers/specs/2026-11-02-large-mailbox-ingest-design.md`. Read it first; this plan argues from it.

## Global Constraints

- Large container = `.pst`/`.ost` with object size **≥ 1,000,000,000 bytes** (`LARGE_CONTAINER_BYTES`). Smaller containers keep today's `readpst` path unchanged.
- No whole-container download and no `/tmp` writes on the large path. Memory is bounded by one chunk.
- Cloud Tasks dispatch deadline is at most **1800 s** (`tasks.MAX_DISPATCH_DEADLINE_S`). A chunk must finish well inside it.
- Every task goes through `tasks._enqueue` so it targets `settings.tasks_target_url` (the worker).
- Target: an 11.2 GB PST fully loaded and searchable in **1–2 hours**.
- Cost: show the estimate before any paid work. Flag plainly when the load estimate is **> $100**.
- No OCR for images **< 20,000 bytes**, with short side **< 150 px**, or inline (`cid:`). The attachment Document is still created and stored.
- PDF attachments get text: PyMuPDF text layer, plus OCR only for pages with **< 10 non-space characters** (reuse the `ingest_pdf.py` gate).
- Per-document AI model: **`claude-haiku-5-5`** (summaries, entities, AI titles). No quality gate (user's decision). Use the Message Batches API when a run has **> 500 documents**.
- AI hold: set when a load contains a large container **or adds > 5,000 documents**. It sticks to the production until someone explicitly runs analysis.
- Alembic: one new revision with `down_revision = "d2l3m4n5o6p7"`. The migration must import only `alembic`/`sqlalchemy`, because CI runs alembic under minimal deps.
- No client data in tests, fixtures, commits or PR text. Use synthetic/ACME data.
- Branch: implementation goes on a fresh branch from `origin/main`, never local `main`.

## Execution Order (combined units)

The 12 task sections below are grouped into **7 units**. Each unit goes to one implementer and one reviewer. Its implementer works through its task sections in order, committing at each task's commit step.

| Unit | Task sections | Depends on | Lane |
|---|---|---|---|
| U1 Speed test | 1 | — | **Parallel:** starts immediately, on its own worktree/branch. |
| U2 Attachments | 8 | — | **Parallel:** starts immediately, on its own worktree/branch. Merged into the main branch before U4 starts. |
| U3 Reader + schema + survey | 2, 3, 4 | — | Main |
| U4 Selection + chunk worker | 5, 6 | U3, U2 merged | Main |
| U5 Finalize | 7 | U4 | Main |
| U6 AI hold + cheaper AI | 9, 10 | U3 (uses `Production.ai_hold`) | Main, after U5. It touches `_finalize_job_if_done` like U5, so it runs sequentially to avoid conflicts. |
| U7 Screens + worker sizing | 11, plus 12 Step 1–2 | U5, U6; U1's constants | Main |

- Until U1 reports, U4 uses provisional `CHUNK_ITEMS = 500` and U7 uses provisional `WORKER_MAX_INSTANCES = 30`. When U1 lands, its constants replace the provisional values in one commit.
- Task 12 Steps 3–5 (PR, deploy, rollout re-run) are done by the controller after the final whole-branch review, not by an implementer.

## Review Focus

1. **A retried or concurrent chunk after a partial commit.** The expected behavior is no duplicate `bates_begin`, no lost messages and no double-counted `items_done`. *Pinned in Task 6* (`test_retry_skips_existing_controls_and_counts_once`).
2. **Two chunks finishing at the same moment.** Finalize must run exactly once. *Pinned in Task 7* (`test_finalize_claims_once`).
3. **A PST with zero selected items** (the uploader unticks everything, or the PST has only contacts). The expected behavior is a valid completion with "0 of 0" and the excluded types listed, not a hang in processing. *Pinned in Task 5* (`test_empty_selection_completes_immediately`).
4. **A message with no subject, no body, a missing date or undecodable headers.** It must still load as a Document. *Pinned in Task 2* (`test_to_parsed_message_tolerates_missing_fields`).
5. **A small load into a held production.** It must not enqueue the pipeline. *Pinned in Task 9* (`test_small_load_into_held_production_does_not_enqueue`).

---

### Task 1: Spike — pypff throughput over a Storage stream

Throwaway measurement that decides the reader mode and chunk size. No product code.

**Files:**
- Modify: `backend/requirements.txt` (add `libpff-python>=20260926`)
- Create: `backend/scripts/spike_pst_throughput.py`
- Create: `docs/superpowers/plans/2026-11-03-large-mailbox-spike-results.md`

**Interfaces:**
- Produces: constants recorded in the results doc and used in Task 2/5/10: `READER_MODE` (`"gcs_stream"` or `"fuse"`), `GCS_READ_CHUNK_BYTES`, `CHUNK_ITEMS`, measured messages/sec per worker, `WORKER_MAX_INSTANCES`.

- [ ] **Step 1:** Get a **public, non-client** sample PST of at least ~1 GB (e.g. an EDRM/Enron PST). Upload it to a scratch path in the bucket, `spike/large-pst/sample.pst`, never under `productions/`.
- [ ] **Step 2:** Write the script. It opens the blob with `blob.open("rb", chunk_size=N)` and `pypff.file().open_file_object(reader)`, then walks the folder tree and reads every message's subject, plain body, headers and attachment bytes. It prints messages/sec and peak RSS. Run it for N ∈ {1 MiB, 8 MiB, 32 MiB} on a Cloud Run job or VM in us-central1, not from a laptop.
- [ ] **Step 3:** If the best result is **< 2 messages/s per worker**, repeat with the bucket mounted via Cloud Storage FUSE and pypff opening the mounted path.
- [ ] **Step 4:** Write the results doc with the four constants and the evidence. `CHUNK_ITEMS` = messages finished in about 10 minutes at the measured rate, rounded down to a multiple of 50, capped at 1000. `WORKER_MAX_INSTANCES` = enough for 75,000 messages in ≤ 90 minutes.
- [ ] **Step 5:** Commit the requirements change, the script and the results doc. `git commit -m "spike(ingest): measure pypff throughput over Storage streams"`

---

### Task 2: `pst_reader` — survey, typed iteration, mapping to ParsedMessage

**Files:**
- Create: `backend/app/services/pst_reader.py`
- Modify: `backend/app/services/email_parse.py` (add `inline_names: set[str] = field(default_factory=set)` to `ParsedMessage`)
- Modify: `backend/app/services/storage.py` (add `open_blob_reader(remote_path: str, chunk_size: int)` returning a seekable file object, and `get_size(remote_path: str) -> int`)
- Test: `backend/tests/test_pst_reader.py`

**Interfaces:**
- Produces:
  - `ITEM_TYPES = ("email", "calendar", "contact", "task", "note", "other")`
  - `item_type(message_class: str | None) -> str`. Mapping: `IPM.Note*`, `IPM.Schedule.Meeting*` (meeting requests) → `email`; `IPM.Appointment*` → `calendar`; `IPM.Contact*`, `IPM.DistList*` → `contact`; `IPM.Task*` → `task`; `IPM.StickyNote*` → `note`; else `other`. Case-insensitive; `None` → `email`.
  - `is_recoverable(folder_path: str) -> bool`. True when any path segment equals `"Recoverable Items"` or `"Deleted Items"` (case-insensitive).
  - `@dataclass FolderSurvey(index: int, path: str, recoverable: bool, counts: dict[str, int], bytes: dict[str, int])`
  - `@dataclass ContainerSurvey(storage_path: str, folders: list[FolderSurvey])`, with `.totals() -> dict[str, int]` and `.to_json() / from_json()`
  - `open_pst(storage_path: str) -> ContextManager[PstHandle]`, which honours `READER_MODE` from Task 1
  - `survey(handle) -> ContainerSurvey`. Folders are in deterministic depth-first order, children in stored sub-folder order, and `index` is that order.
  - `iter_slice(handle, slices: list[list[int]]) -> Iterator[tuple[int, FolderSurvey, int, object]]`. Each slice is `[folder_index, start, end)`, and it yields `(local_seq, folder, item_index, pypff_item)`.
  - `to_parsed(item, kind: str) -> ParsedMessage`

- [ ] **Step 1: Write the failing tests.** Use a fake item tree (simple classes exposing `get_number_of_sub_folders/get_sub_folder/get_number_of_sub_messages/get_sub_message/name/message_class/subject/plain_text_body/html_body/transport_headers/attachments`), not a real PST:

```python
def test_item_type_mapping():
    assert item_type("IPM.Note") == "email"
    assert item_type("IPM.Note.SMIME") == "email"
    assert item_type("IPM.Schedule.Meeting.Request") == "email"
    assert item_type("IPM.Appointment") == "calendar"
    assert item_type("ipm.contact") == "contact"
    assert item_type("IPM.DistList") == "contact"
    assert item_type("IPM.Task") == "task"
    assert item_type("IPM.StickyNote") == "note"
    assert item_type("IPM.Post") == "other"
    assert item_type(None) == "email"

def test_survey_is_deterministic_and_counts_by_type(fake_pst):
    s1, s2 = survey(fake_pst), survey(fake_pst)
    assert [f.path for f in s1.folders] == [f.path for f in s2.folders]
    assert s1.totals() == {"email": 5, "calendar": 2, "contact": 1, "task": 0, "note": 0, "other": 0}
    assert [f.recoverable for f in s1.folders if f.path.endswith("Recoverable Items")] == [True]

def test_iter_slice_yields_exact_range(fake_pst):
    items = list(iter_slice(fake_pst, [[1, 1, 3]]))
    assert [(f.index, i) for _, f, i, _ in items] == [(1, 1), (1, 2)]

def test_to_parsed_message_maps_email_fields(fake_email_item):
    p = to_parsed(fake_email_item, "email")
    assert p.subject == "Q3 numbers" and p.from_ == "alice@acme.test"
    assert p.message_id == "<m1@acme.test>"
    assert ("numbers.csv", b"a,b\n") in p.attachments

def test_to_parsed_message_tolerates_missing_fields(bare_item):
    p = to_parsed(bare_item, "email")   # no subject, body, date or headers
    assert p.subject == "" and p.body_text == "" and p.date_sent is None

def test_to_parsed_renders_calendar_as_text(fake_calendar_item):
    p = to_parsed(fake_calendar_item, "calendar")
    assert "Organizer:" in p.body_text and "Start:" in p.body_text and "Location:" in p.body_text
```

- [ ] **Step 2:** Run `cd backend && python -m pytest tests/test_pst_reader.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement the functions above.
  - Headers come from `transport_headers`, decoded through the existing `email_parse.decode_header_value`.
  - Inline attachments are those whose content-id appears in the HTML body; record them in `inline_names`.
  - Non-email kinds render their properties as labelled lines in `body_text`, with `subject` from the item.
  - `open_blob_reader` uses `blob.open("rb", chunk_size=...)`.
- [ ] **Step 4:** Run the tests. Expected: PASS. Also run `python -m pytest tests/test_email_parse.py tests/test_ingest_native_email.py -q` (ParsedMessage change). Expected: PASS.
- [ ] **Step 5:** Commit. `git commit -m "feat(ingest): stream PSTs with pypff — survey by item type and slice iteration"`

---

### Task 3: Schema — `ingest_chunks`, job progress columns, production `ai_hold`

**Files:**
- Create: `backend/alembic/versions/e3n4o5p6q7r8_large_container_ingest.py` (`revision = "e3n4o5p6q7r8"`, `down_revision = "d2l3m4n5o6p7"`)
- Modify: `backend/app/models.py`
- Test: `backend/tests/test_large_ingest_schema.py`

**Interfaces:**
- Produces:
  - `IngestJob` new columns:
    - `survey` JSONB, default `{}`, keyed by storage_path → `ContainerSurvey.to_json()`
    - `selection` JSONB, default `{}`, keyed by storage_path → `{"types": [...], "include_recoverable": bool}`
    - `items_total` int, default 0
    - `items_done` int, default 0
    - `items_failed` int, default 0
  - New job `status` values: `surveying`, `awaiting_selection`, `finalizing` (plus the existing `processing`/`complete`/`failed`).
  - `Production.ai_hold` Boolean, not null, `server_default=false`.
  - `IngestChunk`, table `ingest_chunks`:
    - `id` UUID pk
    - `job_id` FK → ingest_jobs (cascade), indexed
    - `kind` String(10): `items` | `embed` | `post`
    - `container_path` String(500), nullable
    - `slices` JSONB (list of `[folder_index, start, end]`), default `[]`
    - `doc_ids` JSONB (embed chunks), default `[]`
    - `global_offset` int, default 0
    - `item_count` int, default 0
    - `status` String(12): `pending` | `running` | `done` | `split` | `failed`
    - `attempts` int, default 0
    - `error` Text, nullable
    - `parent_id` UUID, nullable
    - `created_at` / `updated_at`
    - Index on `(job_id, status)`.

- [ ] **Step 1: Write the failing test.** Import the migration module the way CI does, without app deps, and assert the model columns exist:

```python
def test_migration_is_import_safe_and_chained():
    import importlib.util, pathlib
    p = next(pathlib.Path("alembic/versions").glob("e3n4o5p6q7r8_*.py"))
    spec = importlib.util.spec_from_file_location("m", p); m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.down_revision == "d2l3m4n5o6p7"
    src = p.read_text()
    assert "from app" not in src and "import app" not in src

def test_models_have_new_columns():
    from app.models import IngestChunk, IngestJob, Production
    for c in ("survey", "selection", "items_total", "items_done", "items_failed"):
        assert c in IngestJob.__table__.c
    assert "ai_hold" in Production.__table__.c
    assert {"kind", "slices", "doc_ids", "status", "attempts", "parent_id"} <= set(IngestChunk.__table__.c.keys())
```

- [ ] **Step 2:** Run `python -m pytest tests/test_large_ingest_schema.py -q`. Expected: FAIL.
- [ ] **Step 3:** Write the migration and the models.
- [ ] **Step 4:** Run the test. Expected: PASS. Then run `alembic upgrade head` and `alembic downgrade -1` against the local pgvector compose DB. Expected: both succeed.
- [ ] **Step 5:** Commit. `git commit -m "feat(ingest): ingest_chunks table, job item counters, production ai_hold"`

---

### Task 4: Start → survey (detect large containers, survey task, normal batches skip them)

**Files:**
- Modify: `backend/app/routers/ingest.py` (start endpoint, around line 115–260; new `POST /ingest/survey-container`)
- Modify: `backend/app/services/ingest_native.py` (`list_native_sources` adds `"size"`; `ingest_native_batch` skips large containers)
- Modify: `backend/app/services/tasks.py` (`enqueue_survey(job_id: str, storage_path: str)`)
- Modify: `backend/app/config.py` (`large_container_bytes`)
- Create: `backend/app/services/large_ingest.py`
- Test: `backend/tests/test_large_ingest_survey.py`

**Interfaces:**
- Consumes: `pst_reader.open_pst`, `survey`, `ContainerSurvey.to_json` (Task 2); `storage.get_size` (Task 2); Task 3 columns.
- Produces:
  - `settings.large_container_bytes: int = 1_000_000_000` in `backend/app/config.py` (env `VIGILIST_LARGE_CONTAINER_BYTES`, lowered only for local manual testing). `large_ingest.LARGE_CONTAINER_BYTES` reads it.
  - `is_large_container(item: dict) -> bool`. True for `.pst`/`.ost` with `item["size"] >= settings.large_container_bytes`.
  - `async def run_survey(db, job_id: str, storage_path: str) -> None`. Writes `job.survey[storage_path]`. When every large container in the job is surveyed, sets `status = "awaiting_selection"`.
  - `large_source_key(storage_path: str) -> str`, which returns `f"native:{storage_path}"` (the same key format as today).

- [ ] **Step 1: Write the failing tests:**

```python
def test_is_large_container_threshold():
    assert is_large_container({"filename": "a.pst", "size": 1_000_000_000})
    assert not is_large_container({"filename": "a.pst", "size": 999_999_999})
    assert not is_large_container({"filename": "a.zip", "size": 5_000_000_000})
    assert is_large_container({"filename": "B.OST", "size": 2_000_000_000})

async def test_start_with_large_pst_enqueues_survey_and_sets_surveying(...):
    # sources: [small.eml (10 KB), big.pst (11.2e9)]
    # expect: job.status == "processing" for the normal batch path AND one enqueue_survey("…/big.pst");
    # job.field_mapping["large_containers"] == ["…/big.pst"]

async def test_native_batch_skips_large_containers(...):
    # a batch slice that includes big.pst neither downloads it nor marks it done/skipped

async def test_survey_sets_awaiting_selection_when_all_surveyed(...):
    # two large containers; after the first survey status stays "surveying"; after the second, "awaiting_selection"
```

- [ ] **Step 2:** Run `python -m pytest tests/test_large_ingest_survey.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - The start endpoint computes large containers from `list_native_sources` sizes and stores them in `field_mapping["large_containers"]`.
  - Each large container keeps its global index, so its container control number is `{prefix} {offset+index+1:06d}` as today.
  - Batches enqueue as before but skip large items.
  - One survey task is enqueued per large container. The job status is `surveying` if any exist; normal batches still run in parallel.
  - The survey handler is OIDC-protected like `process-batch`.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run the regression set `tests/test_ingest_skip_idempotency.py tests/test_multi_load.py tests/test_email_ingest_robustness.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit. `git commit -m "feat(ingest): survey large PSTs before loading; normal batches skip them"`

---

### Task 5: Estimate + selection → chunk planning and fan-out

**Files:**
- Create: `backend/app/services/large_ingest_estimate.py`
- Modify: `backend/app/services/large_ingest.py`
- Modify: `backend/app/routers/ingest.py` (`POST /ingest/{job_id}/selection`, owner-only)
- Modify: `backend/app/services/tasks.py` (`enqueue_chunk(chunk_id: str)`)
- Test: `backend/tests/test_large_ingest_plan.py`

**Interfaces:**
- Consumes: `ContainerSurvey`, `FolderSurvey` (Task 2); `IngestChunk` (Task 3).
- Produces:
  - `estimate_load(survey: ContainerSurvey, types: set[str], include_recoverable: bool) -> dict`. Returns keys `items`, `bytes`, `minutes`, `usd`, `storage_gb`, `over_budget` (`usd > 100`). The cost constants live at the top of the module, each with a one-line source comment: compute per item from the spike, Voyage embeddings per byte of text, and Vision OCR assuming 10% of attachment bytes are images over the tiny threshold.
  - `plan_chunks(survey, types, include_recoverable, chunk_items: int) -> list[dict]`. Each dict is `{"slices": [[folder_index, start, end], ...], "global_offset": int, "item_count": int}`.
    - It walks folders in index order and keeps only selected items. Item positions within a folder are 0..count-1. Type filtering happens at read time (Task 6), so a slice counts *raw* positions while `item_count` and `global_offset` count *selected* items.
    - To keep that exact, `plan_chunks` needs per-folder positions by type. If a folder mixes selected and unselected types, store a `type_filter` list in the chunk (add a key to the dict) that the chunk applies when reading.
    - `global_offset` is the number of selected items before this chunk, which makes numbering deterministic.
  - `async def apply_selection(db, job_id: str, storage_path: str, types: list[str], include_recoverable: bool) -> dict`. Writes `job.selection`, the chunk rows and `items_total`. Sets status `processing` and enqueues every chunk. Returns the estimate. A selection already applied returns 409.

- [ ] **Step 1: Write the failing tests:**

```python
def test_plan_chunks_covers_selected_items_exactly_once(sample_survey):
    chunks = plan_chunks(sample_survey, {"email", "calendar"}, include_recoverable=True, chunk_items=3)
    assert sum(c["item_count"] for c in chunks) == sample_survey_total(sample_survey, {"email", "calendar"}, True)
    assert [c["global_offset"] for c in chunks] == list(itertools.accumulate([0] + [c["item_count"] for c in chunks[:-1]]))

def test_plan_chunks_excludes_recoverable_when_unchecked(sample_survey):
    chunks = plan_chunks(sample_survey, {"email"}, include_recoverable=False, chunk_items=100)
    rec = {f.index for f in sample_survey.folders if f.recoverable}
    assert all(s[0] not in rec for c in chunks for s in c["slices"])

def test_estimate_flags_over_budget(big_survey):
    assert estimate_load(big_survey, {"email"}, True)["over_budget"] is False   # ~75k emails ≈ $35–80
    assert set(estimate_load(big_survey, {"email"}, True)) >= {"items", "minutes", "usd", "storage_gb", "over_budget"}

async def test_empty_selection_completes_immediately(...):
    # types=[] → no chunks, items_total=0, job proceeds to finalize (Task 7) without hanging in "processing"
```

- [ ] **Step 2:** Run `python -m pytest tests/test_large_ingest_plan.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. `chunk_items` defaults to `CHUNK_ITEMS` from Task 1.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit. `git commit -m "feat(ingest): estimate and plan chunked loads from the uploader's type selection"`

---

### Task 6: Chunk worker — stream, build, commit per message, split on repeated failure

**Files:**
- Modify: `backend/app/services/large_ingest.py`
- Modify: `backend/app/services/ingest.py` (add `append_job_errors`, `incr_job_items`)
- Modify: `backend/app/routers/ingest.py` (`POST /ingest/process-chunk`, OIDC)
- Test: `backend/tests/test_large_ingest_chunk.py`

**Interfaces:**
- Consumes: `iter_slice`, `to_parsed`, `item_type` (Task 2); `build_email_documents`, `_store_attachment`, `_stamp_source`/`_source_stamp`, `_persist_documents` (existing, `ingest_native.py` / `ingest.py`); `should_ocr_image` / `extract_attachment` (Task 8). Until Task 8 lands, pass today's `extract`.
- Produces:
  - `MAX_CHUNK_ATTEMPTS = 3`
  - `message_control(container_control: str, position: int, total: int) -> str`, returning `f"{container_control} -{position:0{w}d}"` with `w = max(4, len(str(total)))`. Positions are 1-based global selected positions.
  - `async def append_job_errors(db, job_id: str, messages: list[str]) -> None`. Does `UPDATE ingest_jobs SET errors = errors || :json` atomically and never overwrites.
  - `async def incr_job_items(db, job_id: str, done: int = 0, failed: int = 0) -> None`, an atomic increment.
  - `async def run_chunk(db, chunk_id: str) -> None`. Steps:
    1. Claim: in one statement, `attempts+1, status='running'` where status is `pending` or `running`. A `done`, `split` or `failed` chunk returns immediately.
    2. If `attempts > MAX_CHUNK_ATTEMPTS`:
       - with `item_count > 1`, split into two child chunks (same `kind`, halves of the selected range, `parent_id` set), set the parent to `split`, enqueue both, and return;
       - with `item_count == 1`, record the item as failed (folder path, position, subject if readable) through `append_job_errors` and `incr_job_items(failed=1)`, set status `failed`, and go to finalize.
    3. Otherwise read the chunk's controls that already exist (one query, `bates_begin IN (...)`) and stream the slice. For each selected item: skip it if it exists; otherwise build docs with `metadata_["PST Folder"] = folder.path`, persist the family with `_persist_documents`, then `incr_job_items(done=1)`. A Python exception on one item records that item as failed and continues.
    4. Set status `done`, then call `maybe_finalize(db, job_id)` (Task 7).
  - A worker crash (OOM or timeout) leaves the chunk `running`. The Cloud Tasks retry re-claims it, and `attempts` drives the split.

- [ ] **Step 1: Write the failing tests** (FakeSession + fake reader):

```python
def test_message_control_width_grows_with_total():
    assert message_control("ACME 000007", 12, 9_999) == "ACME 000007 -0012"
    assert message_control("ACME 000007", 12, 75_000) == "ACME 000007 -00012"

async def test_chunk_builds_documents_with_folder_and_controls(...):
    # 3 emails in slice → 3 parent Documents, bates "ACME 000007 -0001..0003", metadata_["PST Folder"] == "Top/Inbox"

async def test_retry_skips_existing_controls_and_counts_once(...):
    # controls -0001,-0002 already exist → only -0003 persisted; incr_job_items called with done=1 once

async def test_fourth_attempt_splits_chunk(...):
    # attempts already 3, item_count 10 → two children with item_count 5/5, parent status "split", 2 enqueues

async def test_single_item_chunk_over_attempts_is_recorded_failed(...):
    # item_count 1, attempts 3 → append_job_errors called with folder/position text, items_failed += 1, status "failed"

async def test_one_bad_item_does_not_fail_chunk(...):
    # to_parsed raises for item 2 of 3 → items 1 and 3 persisted, one error appended, chunk "done"

async def test_unselected_types_in_folder_are_skipped(...):
    # chunk type_filter=["email"]; folder holds email, contact, email → 2 Documents, positions contiguous
```

- [ ] **Step 2:** Run `python -m pytest tests/test_large_ingest_chunk.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run `tests/test_ingest_native_email.py tests/test_ingest_stamp_after_rollback.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit. `git commit -m "feat(ingest): chunk worker streams PST slices, commits per message, splits failing ranges"`

---

### Task 7: Atomic finalize, reconciliation, embed/post fan-out, completion

**Files:**
- Modify: `backend/app/services/large_ingest.py`
- Modify: `backend/app/services/ingest.py` (`_finalize_job_if_done` delegates when `field_mapping["large_containers"]` is set)
- Modify: `backend/app/routers/ingest.py` (`POST /ingest/process-chunk` handles all `kind`s)
- Test: `backend/tests/test_large_ingest_finalize.py`

**Interfaces:**
- Consumes: Task 6 chunk rows; `chunk_and_embed_document` (`services/embeddings.py:109`); `derive_threads`, title generation (as called today in `_finalize_job_if_done`).
- Produces:
  - `EMBED_BATCH_DOCS = 200`
  - `async def maybe_finalize(db, job_id: str) -> None`, which runs the phases below.
    - **Phase A.** Starts when no `items` chunk is pending, running or split-with-pending-children, and the normal sources are all attempted.
      - It claims with one conditional `UPDATE ingest_jobs SET status='finalizing' WHERE id=:id AND status='processing'`. Only `rowcount == 1` proceeds.
      - Mark each large container's source key done.
      - Append the reconciliation line per container: `"{name}: {loaded:,} of {selected:,} loaded, {failed:,} could not be read; excluded by uploader: {type} {n:,}, …; deep recovery of orphaned items was not performed"`.
      - Set `production.ai_hold = True`.
      - Create `embed` chunks over the job's documents without chunks, `EMBED_BATCH_DOCS` per chunk, and enqueue them.
    - **Phase B.** When no `embed` chunk is pending, create one `post` chunk, claimed the same way.
    - **The post chunk** runs `derive_threads` and title generation for untitled docs, then sets `status='complete'` and `completed_at`. It never enqueues the pipeline.
  - `run_chunk` dispatches on `kind`: `items` (Task 6), `embed` (embeds its `doc_ids`), `post`.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_finalize_claims_once(...):
    # two concurrent maybe_finalize calls; responder returns rowcount 1 then 0 → embed chunks created once

async def test_finalize_waits_for_pending_items(...):
    # one items chunk still "running" → no claim, status stays "processing"

async def test_reconciliation_line_lists_exclusions(...):
    # selected 10 (8 loaded, 2 failed), contacts 4 excluded →
    # "big.pst: 8 of 10 loaded, 2 could not be read; excluded by uploader: contact 4; deep recovery of orphaned items was not performed"

async def test_complete_only_after_embed_and_post(...):
    # status "finalizing" until the post chunk finishes; then "complete"; enqueue_pipeline never called; production.ai_hold True
```

- [ ] **Step 2:** Run `python -m pytest tests/test_large_ingest_finalize.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run `tests/test_ingest_conn_release.py tests/test_embeddings.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit. `git commit -m "feat(ingest): atomic finalize with reconciliation; embeddings and threading as fanned-out tasks"`

---

### Task 8: Attachments — skip OCR on tiny/inline images, extract PDF attachment text

**Files:**
- Modify: `backend/app/services/extractors.py`
- Modify: `backend/app/services/ingest_native.py` (`build_email_documents` uses the new wrapper for every email path)
- Test: `backend/tests/test_attachment_extraction.py`

**Interfaces:**
- Produces:
  - `TINY_IMAGE_BYTES = 20_000`, `TINY_IMAGE_SIDE_PX = 150`
  - `should_ocr_image(name: str, data: bytes, inline: bool) -> bool`. Returns False if `inline`, if `len(data) < TINY_IMAGE_BYTES`, or if the PIL-measured short side is `< TINY_IMAGE_SIDE_PX`. Unreadable image → True.
  - `extract_pdf_text(data: bytes, ocr_fn) -> ExtractResult`. PyMuPDF text per page; pages under the `ingest_pdf.py` <10-character gate are rendered at the same DPI and sent to `ocr_fn`. Result `file_type="pdf"`.
  - `extract_attachment(name: str, data: bytes, *, inline: bool, ocr_fn) -> ExtractResult`. Routes `.pdf` to `extract_pdf_text`. Images go to OCR only if `should_ocr_image`; otherwise the result is empty text with `extraction_status="ok"`. Everything else goes to `extract`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_tiny_and_inline_images_skip_ocr(png_bytes_factory):
    assert not should_ocr_image("logo.png", png_bytes_factory(10_000, (400, 400)), inline=False)
    assert not should_ocr_image("sig.png", png_bytes_factory(50_000, (600, 120)), inline=False)
    assert not should_ocr_image("scan.png", png_bytes_factory(500_000, (2000, 2000)), inline=True)
    assert should_ocr_image("scan.png", png_bytes_factory(500_000, (2000, 2000)), inline=False)

def test_build_email_documents_does_not_ocr_tiny_inline_image():
    calls = []
    # parsed with attachments [("image001.png", tiny_png)], inline_names={"image001.png"}
    # → attachment Document exists, calls == []

def test_pdf_attachment_gets_text_layer_and_ocr_only_blank_pages(two_page_pdf_one_scanned):
    ocr_calls = []
    res = extract_pdf_text(two_page_pdf_one_scanned, ocr_fn=lambda img: ocr_calls.append(1) or "SCANNED TEXT")
    assert "typed page text" in res.text and "SCANNED TEXT" in res.text and len(ocr_calls) == 1
```

- [ ] **Step 2:** Run `python -m pytest tests/test_attachment_extraction.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Generate test PDFs and PNGs in fixtures with PyMuPDF/PIL at test time; don't commit binaries.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run `tests/test_extractors.py tests/test_extractors_new_formats.py tests/test_ingest_native_email.py tests/test_ingest_native_images.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit. `git commit -m "feat(ingest): no OCR for tiny/inline images; text for PDF attachments"`

---

### Task 9: Sticky AI hold + run-on-request with estimate

**Files:**
- Modify: `backend/app/services/ingest.py` (`_finalize_job_if_done`)
- Modify: `backend/app/routers/ingest.py` (the pipeline-run endpoint behind `runPipeline` in `frontend/src/api/client.ts:659`, and `extract-entities`, both clear the hold; new `GET /productions/{production_id}/ai-estimate`)
- Test: `backend/tests/test_ai_hold.py`

**Interfaces:**
- Produces:
  - `AI_HOLD_DOC_THRESHOLD = 5_000`
  - `should_hold(production, docs_added: int, had_large_container: bool) -> bool`. True if `production.ai_hold`, if `had_large_container`, or if `docs_added > AI_HOLD_DOC_THRESHOLD`.
  - `GET /productions/{id}/ai-estimate` returns `{"held": bool, "documents_pending": int, "usd_estimate": float}`. `documents_pending` = docs with text and no summary or `entities_extracted_at`. The USD uses Haiku 5.5 batch pricing ($0.05 / $0.25 per MTok, half of $0.10/$0.50), with input tokens ≈ text chars / 4 (summaries capped at 12,000 chars) and output ≈ 300 + 1,000 tokens per doc.
  - Explicit run endpoints set `ai_hold = False` before enqueueing.

- [ ] **Step 1: Write the failing tests:**

```python
def test_should_hold_rules():
    p = SimpleNamespace(ai_hold=False)
    assert should_hold(p, 5_001, False) and not should_hold(p, 5_000, False)
    assert should_hold(p, 10, True)
    assert should_hold(SimpleNamespace(ai_hold=True), 1, False)

async def test_small_load_into_held_production_does_not_enqueue(...):
    # production.ai_hold True, normal load of 3 docs finalizes → enqueue_pipeline not called, ai_hold stays True

async def test_large_normal_load_sets_hold(...):
    # 6,000 docs added → ai_hold True, no enqueue

async def test_run_endpoint_clears_hold_and_enqueues(...):
    # POST run → ai_hold False, enqueue_pipeline called once
```

- [ ] **Step 2:** Run `python -m pytest tests/test_ai_hold.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run `tests/test_pipeline.py tests/test_pipeline_staleness.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit. `git commit -m "feat(pipeline): sticky AI hold for large loads; run on request with cost estimate"`

---

### Task 10: Haiku 5.5 for per-document stages + Message Batches for big runs; timeline cap

**Files:**
- Modify: `backend/app/services/ai.py` (`PER_DOC_MODEL = "claude-haiku-5-5"`; used in summaries/titles, lines ~173–284)
- Modify: `backend/app/services/entity_extraction.py:334` (`EXTRACTION_MODEL = PER_DOC_MODEL`)
- Create: `backend/app/services/claude_batch.py`
- Modify: `backend/app/services/pipeline.py` (summaries/entities stages use batches above the threshold; a waiting state)
- Modify: `backend/app/services/timeline_review.py` (event cap)
- Modify: `backend/app/services/tasks.py` (`enqueue_batch_poll(production_id: int, stage: str, delay_s: int)`, using `schedule_time`)
- Modify: `backend/app/routers/ingest.py` (`POST /ingest/poll-batch`, OIDC)
- Test: `backend/tests/test_claude_batch.py`, `backend/tests/test_timeline_cap.py`

**Interfaces:**
- Produces:
  - `BATCH_THRESHOLD_DOCS = 500`, `BATCH_POLL_SECONDS = 300`
  - Requests to `claude-haiku-5-5` send `thinking={"type": "disabled"}` and `output_config={"effort": "low"}`, and send **no** `temperature`/`top_p`/`top_k` (removed on API version 2026-10-21; delete any existing ones in these call sites).
  - `claude_batch.submit(requests: list[tuple[str, dict]]) -> str` (batch id; `custom_id` = document id)
  - `claude_batch.fetch_if_done(batch_id: str) -> dict[str, str | None] | None`. `None` while processing; otherwise `custom_id → text`, or `None` for an errored/expired item.
  - Pipeline stage contract: a batched stage stores `{"batch_id": ..., "submitted_at": ...}` under `ai_pipeline_status[stage]` with status `"waiting"`, then enqueues a poll. The poll handler applies results through the same persistence code the synchronous path uses and marks the stage `done`, then continues `run_ambient_pipeline`. Errored items stay unprocessed and are retried synchronously on the next run.
  - `MAX_TIMELINE_EVENTS = 3_000`. Above it, timeline review keeps a deterministic subset (highest mention count, then earliest date) and records `"sampled": true` and the counts in the stage status.

- [ ] **Step 1: Write the failing tests:**

```python
def test_per_doc_model_is_haiku_5_5():
    from app.services import ai, entity_extraction
    assert ai.PER_DOC_MODEL == "claude-haiku-5-5" and entity_extraction.EXTRACTION_MODEL == "claude-haiku-5-5"

def test_per_doc_requests_disable_thinking_and_send_no_sampling(captured_request):
    # call generate_summary with a fake client → kwargs have thinking disabled, effort low, no temperature

async def test_large_summary_run_submits_batch_and_waits(...):
    # 600 pending docs → claude_batch.submit called once with 600 requests; stage status "waiting"; poll enqueued

async def test_poll_applies_results_and_marks_done(...):
    # fetch_if_done returns results → summaries persisted for successful ids; stage "done"; errored ids left unsummarized

async def test_poll_reschedules_while_processing(...):
    # fetch_if_done returns None → enqueue_batch_poll called again with delay 300

def test_timeline_cap_is_deterministic():
    ev = [fake_event(i) for i in range(5_000)]
    a, b = cap_events(ev), cap_events(list(reversed(ev)))
    assert len(a) == 3_000 and [e.id for e in a] == [e.id for e in b]
```

- [ ] **Step 2:** Run `python -m pytest tests/test_claude_batch.py tests/test_timeline_cap.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement with the SDK's `client.messages.batches.create/retrieve/results`, keying results by `custom_id`, never by position.
- [ ] **Step 4:** Run the tests. Expected: PASS. Run `tests/test_entity_extract_llm.py tests/test_entity_pipeline.py tests/test_pipeline.py tests/test_brief.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit. `git commit -m "feat(pipeline): Haiku 5.5 per-document stages, Message Batches for large runs, timeline cap"`

---

### Task 11: Status API + wizard selection screen + item progress + matter notice

**Files:**
- Modify: `backend/app/schemas.py` (`IngestJobOut`, around line 440)
- Modify: `backend/app/routers/ingest.py` (status endpoint around line 424; `GET /productions/{id}/ingest-awaiting`)
- Modify: `frontend/src/api/client.ts`, `frontend/src/types.ts` (or wherever `IngestJob` is typed)
- Create: `frontend/src/components/ContainerSelection.tsx`
- Modify: `frontend/src/components/IngestWizard.tsx` (polling around line 235, progress around line 304–330)
- Modify: `frontend/src/components/ProductionBrief.tsx` (held banner + estimate + Run)
- Test: `backend/tests/test_large_ingest_status.py`

**Interfaces:**
- Consumes: Task 3 columns; `estimate_load` (Task 5); `GET /productions/{id}/ai-estimate` (Task 9).
- Produces:
  - `IngestJobOut` adds:
    - `items_total: int = 0`, `items_done: int = 0`, `items_failed: int = 0`
    - `containers: list[ContainerOut] = []`, where `ContainerOut` has `storage_path`, `name`, `size_bytes`, `types: list[{type, count, bytes}]`, `recoverable: {count, bytes}`, `selected: bool`, and `estimate` (the `estimate_load` dict for the default selection).
  - `GET /productions/{id}/ingest-awaiting` → `[{job_id, name, size_bytes}]` for jobs in `awaiting_selection` that the caller owns.
  - Frontend:
    - When status is `awaiting_selection`, the wizard renders `<ContainerSelection>`: one row per type with count and size, checkboxes (defaults: Email ✓, Calendar ✓, Recoverable/Deleted ✓, others ☐), a live estimate (minutes, $, storage GB) recomputed client-side from the per-type numbers returned, an over-$100 warning line, and Confirm → `POST /ingest/{job_id}/selection`.
    - While processing a large container, the progress shows `"{items_done:,} of {items_total:,} messages"`, with the bar at `items_done/items_total`.
    - On complete, show the reconciliation line.
    - The production page shows a notice linking back to selection when `ingest-awaiting` is non-empty, and a "AI analysis not run — estimated $X — Run" banner when `held`.

- [ ] **Step 1: Write the failing backend test:**

```python
async def test_status_exposes_items_and_container_survey(...):
    # job awaiting_selection with one surveyed container →
    # out.containers[0].types includes {"type": "email", "count": 5, ...}; estimate present; items_total == 0
    # after selection: items_total == 7; items_done reflects counter
```

- [ ] **Step 2:** Run `python -m pytest tests/test_large_ingest_status.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the backend, then the frontend. The frontend has no test suite and lints red on main. Follow the React Compiler rules: no refs or setState during render.
- [ ] **Step 4:** Run the backend test. Expected: PASS. Run `cd frontend && npx tsc --noEmit`. Expected: no new errors in touched files. Run `npx eslint src/components/ContainerSelection.tsx src/components/IngestWizard.tsx src/components/ProductionBrief.tsx`. Expected: no new errors versus main. Manual check with the local dev env: upload a synthetic PST larger than a temporarily lowered threshold (env override `VIGILIST_LARGE_CONTAINER_BYTES`, read in `large_ingest.py`). See the selection screen, confirm, and watch the counts reach "Complete" with the reconciliation line.
- [ ] **Step 5:** Commit. `git commit -m "feat(ui): choose what to bring in from a large mailbox; message-level progress; AI hold banner"`

---

### Task 12: Worker sizing + rollout

**Files:**
- Modify: `.github/workflows/backend-deploy.yml` (the `vigilist-worker` deploy step)

- [ ] **Step 1:** Set the worker to `--concurrency 1`, `--max-instances {WORKER_MAX_INSTANCES from Task 1}`, `--cpu 2`, `--memory 4Gi`, `--timeout 1800`. Leave `vigilist-api` unchanged.
- [ ] **Step 2:** Run the full backend suite: `cd backend && python -m pytest -q`. Expected: same pass/fail set as `origin/main` plus the new tests passing. Note any pre-existing failures in the PR.
- [ ] **Step 3:** Commit, open the PR (no client data in the description), merge, and watch the deploy. The alembic upgrade runs automatically in the deploy.
- [ ] **Step 4: Rollout verification.** Start a new native ingest of the existing failed upload (production 16, `load_prefix` `loads/74bf0d12/`) through the UI as the uploader. Then confirm:
  - (a) the survey finishes and the selection screen shows per-type counts and sizes;
  - (b) after confirming, worker memory stays under the limit (no "Memory limit … exceeded" in `vigilist-worker` logs);
  - (c) it completes in ≤ 2 hours;
  - (d) the reconciliation line accounts for every selected item;
  - (e) the AI hold banner shows, with an estimate;
  - (f) Cloud Run, Vision and Voyage spend for the run is ≤ $100 (use the GCP cost queries in memory).
- [ ] **Step 5:** Record the measured time, cost and counts in the PR, plus a short follow-up note in the spec.
