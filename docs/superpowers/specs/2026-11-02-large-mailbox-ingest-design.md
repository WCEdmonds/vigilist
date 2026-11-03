# Large-mailbox ingest — design

Date: 2026-11-02
Status: draft for review

## Why

A reviewer uploaded a single 11.2 GB Outlook PST to a matter. The upload
never loaded. The `vigilist-worker` Cloud Run service (4 GiB) ran out of
memory about 80 seconds into the batch, every time. Cloud Tasks retried it
hourly (19 attempts) until we deleted the task by hand on 2026-11-02.

Today an email container is expanded **entirely inside one task**, and is
held in memory roughly four times over:

1. the full container download (`get_download_bytes`);
2. a temp copy for `readpst` (Cloud Run's `/tmp` is RAM);
3. `readpst`'s exploded `.eml` files, also in `/tmp`;
4. every `ParsedMessage` with attachment bytes, plus every `Document`,
   committed in one transaction.

See `backend/app/services/ingest_native.py` (`process_native_email`,
`_expand_email_bytes`, `ingest_native_batch`) and
`backend/app/services/email_parse.py` (`_explode_pst`). Cloud Run tops out at
32 GiB, so adding memory can't fix a mailbox this size. The work has to be
streamed and split up.

## What the user wants

- **Discovery-grade replica.** The mailbox will be used for discovery. Every
  message and every attachment must load, with families intact (parent email
  plus its attachments). Nothing may be silently dropped.
- **Searchable, fast.** The reviewer needs to search these emails. An
  11.2 GB mailbox should be fully loaded and searchable in **about one to two
  hours**.
- **All at once, not a trickle.** Partial availability while loading
  doesn't help the reviewer. What matters is finishing quickly and saying
  clearly when it's complete.
- **Cost.** Check with the user before anything that would cost more than
  **$100 for an 11 GB upload**.
- **AI analysis held, and cheaper.** For large loads, the ambient AI
  pipeline (summaries, entities, timeline review, brief) must **not** run
  automatically. It runs only when someone asks. When it does run, the
  per-document stages use the cheaper setup (see "AI pipeline" below).
- **No OCR on tiny inline images.** Signature images and logos must not be
  sent to Cloud Vision.
- **The uploader chooses what to bring in.** A PST holds more than email.
  Before processing starts, show the uploader each item type it contains
  (email, calendar, contacts, tasks, notes) with its count and size, and let
  them pick which types to load.

## Goals

1. Load a multi-GB PST/OST with memory use bounded by chunk size, not by
   mailbox size.
2. Process one container in parallel across many worker instances, so an
   11 GB PST finishes in one to two hours.
3. Prove completeness. Reconcile the number of messages loaded against the
   PST's own message counts, and list every message that couldn't be read
   by folder, position and subject.
4. Keep each message's PST folder path (Inbox, Sent Items, subfolders).
5. Show real progress ("38,000 of 61,000 messages") and a clear "complete".
6. Keep the cost of loading plus search for an 11 GB PST under $100. Hold the
   AI pipeline for large loads until someone requests it.

## Non-goals

- Making emails searchable gradually while loading. Rows may become
  visible as chunks commit, but no UI work goes into partial availability.
- Changing how small containers (`.eml`, `.msg`, small PSTs, `.mbox`) load,
  beyond what's needed to share code.
- The "guard" work: skipping batches whose job already failed, and capping
  per-file retries. It's a separate, later PR, per the user.
- Re-designing the AI pipeline beyond the hold, the cheaper model, and making
  timeline review survive large event counts.

## Anti-goals (failure even if it "works")

- Silently losing messages, as `readpst -j` parallel mode once did (#112),
  or skipping a whole container because one chunk committed (see the
  dedup hazard below).
- Marking the job complete while chunks are still writing.
- A poison message or chunk that crash-loops the worker for days.
- Surprise AI spend on a large load.
- Starving the web service. All of this work stays on `vigilist-worker`.

## Constraints and facts

- Uploads live in the Firebase Storage bucket (`settings.firebase_storage_bucket`),
  same region as Cloud Run (us-central1).
- Cloud Tasks dispatch deadline is at most 30 minutes per task
  (`MAX_DISPATCH_DEADLINE_S = 1800` in `backend/app/services/tasks.py`).
- `vigilist-worker`: 4 GiB, concurrency 4, maxScale 5 today. The deploy is
  defined in `.github/workflows/backend-deploy.yml`, so sizing changes go
  there.
- Postgres is Neon (Azure eastus2, pooled endpoint). The parallel worker count
  must respect its connection limit and its data-transfer cost (see the
  2026-07-28 quota incident in project history).
- `libpff-python` (pypff) publishes manylinux cp313 wheels (20260926), so it
  installs in the existing `python:3.13-slim` image without a compiler.
- No client data in git. Tests and fixtures use synthetic mailboxes only.

## Design

### 1. Read the PST without loading it

Use **pypff**, opened on a **seekable stream over the Storage object**
(`pypff.file().open_file_object(...)` given a `google.cloud.storage` blob
reader, `blob.open("rb")`, with a generous `chunk_size`). Nothing is
downloaded whole, and nothing is written to `/tmp`. pypff exposes the folder
tree with per-folder message counts, and lets us read message *i* of folder
*F* directly. That is what makes ranges and reconciliation possible.

This applies to `.pst`/`.ost` files above a size threshold. *My call:* 1 GB.
Smaller PSTs keep today's `readpst` path. Once the new path is proven, it may
take over all PSTs, but not in this project.

**Risk to retire first (spike):** random-access throughput of pypff over a
Storage reader. If it's too slow, the fallback is a Cloud Storage FUSE volume
mount on `vigilist-worker` with pypff opening the mounted file. That still
needs no RAM copy and no `/tmp`. The plan's first step measures messages per
second per worker on a synthetic or public-sample PST, and picks the approach.

### 2. Plan, then fan out

When a native load contains a large PST:

1. **Survey task** (one per large container). Open the PST and walk the
   folder tree in a deterministic order. For each folder, record:
   - its path;
   - its item count, broken down by **item type** (email, calendar,
     contact, task, note, other), using the pypff item type / message class;
   - its approximate size per type.

   Store the survey on the job, compute a rough time and cost estimate for
   each type, and move the job to a new status, **awaiting selection**. No
   paid work happens before the uploader chooses.
2. **Selection** (uploader, see §2a). Once the uploader confirms the types:
   - build the ordered sequence of the selected items only;
   - split it into **chunks** sized to finish well inside 30 minutes. *My
     call:* start around 500 items and tune from the spike;
   - write one row per chunk to a new table (e.g. `ingest_chunks`: job,
     container storage path, ordered list of folder/start/end slices, global
     offset, status, attempts, error);
   - enqueue one Cloud Task per chunk;
   - record the selected total on the job.
3. **Chunk task.** Open the PST again (streamed) and read exactly its slice.
   For each message:
   - build a `ParsedMessage` from the pypff item;
   - turn it into Documents via the existing `build_email_documents`;
   - store attachments via `_store_attachment`;
   - commit in small transactions (every N messages) so memory stays flat.

   Mark the chunk done at the end.
4. **Completion** follows the production-set render pattern
   (`routers/production_sets.py` + `services/production_render.py`): the job is
   done when no chunk is pending. Finalize must be **atomic** (row lock or
   conditional update) so it runs exactly once.

### 2a. "What do you want to bring in?" screen

The ingest wizard shows a screen for a surveyed container, in the uploader's
flow. They may have left; a matter-level notice brings them back to it.

- One row per item type present: **Email, Calendar, Contacts, Tasks,
  Notes**, plus "Other" if any. Each row shows its count and size, and has a
  checkbox. *My call:* Email and Calendar are checked by default.
- A separate row for the **Recoverable Items / Deleted Items** folders, if
  present, with count and size. *My call:* checked by default, so nothing is
  silently dropped.
- An estimated load time and cost for the current selection. If the cost
  is above $100, say so plainly before the uploader confirms. This is how the
  $100 rule is met.
- A confirm button that starts the chunked load.

Non-email types become documents through the same pipeline. The builder
decides how each type renders as text and metadata (calendar: organizer,
attendees, start/end, location, body; contact: name, emails, phones,
company; task/note: subject, dates, body). Orphaned items outside the folder
tree, which pypff can recover, are **out of scope** for now. The summary says
deep recovery wasn't performed.

### 3. Numbering and idempotency

- Message control numbers come from the message's **global position** in the
  deterministic traversal, not a per-task `enumerate`. Format:
  `{container control} -{position}`, zero-padded wide enough for the
  container's total (today it's 4 digits, too few for 75k messages).
  Attachments keep the ` .NNNN` suffix. `bates_begin` stays under its 50-char
  limit.
- Idempotency is **per message**. A retried chunk skips messages whose
  control number already exists, and each message's family commits
  all-or-nothing. The large-container path must **not** use today's
  container-level `native_path` dedup. Under parallel chunks, that check
  would skip the entire container as soon as any chunk commits.
- The ingest job's single native `done_keys` entry for the PST is written
  only at finalize, not by the first chunk to finish.

### 4. Folder path

Store each message's PST folder path, for example in `metadata_` under a new
key such as `"PST Folder"`. Keep the existing `"Folder"` key (the upload
folder) unchanged. Whether it's also a filterable column is the builder's
call.

### 5. Completeness and errors

- At finalize, compare the number of items **selected** (from the survey)
  with the items loaded plus the items that failed. Types the uploader
  didn't select are listed as "excluded by uploader" with their counts, not
  as failures.
- Every message that couldn't be read or built is recorded with its folder
  path, position and subject (if readable). The job's warnings list them, and
  the completion summary shows "61,204 of 61,210 loaded, 6 could not be
  read".
- Job errors must **accumulate atomically** across parallel chunks. Today
  `_persist_job_errors` overwrites with each task's snapshot, so the last
  writer wins.
- A chunk that fails repeatedly must not crash-loop forever, and one bad
  message must not cost its neighbours. *My call:* after a few attempts, a
  failing range is **split** into smaller ranges. Attempts are tracked per
  range. Splitting continues until a single item fails on its own, and only
  that item is recorded as failed. Then the job finishes. Design the chunk
  table for sub-ranges up front. (This is narrower than the deferred general
  guard. It is required here because this path creates many tasks.)

### 6. Finalize off the hot path

`_finalize_job_if_done` currently runs AI titles, `embed_production_documents`
and `derive_threads` **inline** in one request. At roughly 115k documents
that won't fit in a 30-minute task. For this path, finalize marks the job
complete and **enqueues** the heavy follow-ups as their own tasks.
Embeddings in particular are fanned out in batches, since search depends on
them. "Complete" in the UI means emails, attachments and search embeddings
are done.

### 7. Attachments

- **Tiny inline images: no OCR.** The attachment document is still created
  and stored (replica), but small images skip Cloud Vision. *My call:* under
  roughly 20 KB, or under roughly 150 px on the short side, plus inline
  (`cid:`) images. The exact rule is the builder's.
- **PDF attachments get text.** Today the email path doesn't extract PDF
  attachments at all (`extractors.extract` treats `.pdf` as unsupported), so
  they aren't searchable. *My call:* extract embedded text with PyMuPDF, and
  OCR only pages without a text layer, reusing the existing <10-character gate
  from `ingest_pdf.py`. Scanned-page OCR counts toward the cost estimate
  below.

### 8. Worker sizing

Each chunk task holds one chunk's messages, not the mailbox. Raise
`vigilist-worker` max instances enough to hit one to two hours, and lower
per-instance concurrency so one heavy chunk can't sink its neighbours. The
builder sets the numbers from the spike's throughput, bounded by Neon
connection limits. Changes go in `.github/workflows/backend-deploy.yml`.

### 9. Progress UI

For a load containing a large container, the ingest status reports **messages
done / total messages** (from chunk progress) instead of files, so the bar
moves. The current one-file PST sits at 0% until it jumps to 100%. Show
"Complete" plus the reconciliation line when finished. The relevant code is
`frontend/src/components/IngestWizard.tsx` and `GET /api/ingest/{id}/status`
(`routers/ingest.py`).

### 10. AI pipeline: hold and cheapen

- **Hold.** A load counts as "large" when it contains a large container.
  *My call:* also when it adds more than 5,000 documents. A large load sets a
  **hold on the production**. While the hold is on:
  - no finalize enqueues the ambient pipeline, including later small loads
    into the same matter. Those would otherwise sweep up all the held
    documents, because the pipeline stages cover every unprocessed document
    in the production;
  - the production shows that AI analysis hasn't run, with an action to run
    it that displays an estimated cost first.

  Running analysis clears the hold. Productions without a hold keep today's
  automatic behavior.
- **Cheaper setup.** The per-document stages (summaries in `pipeline.py`/`ai.py`,
  entities in `entity_extraction.py`, AI titles) move from `claude-haiku-4-5` to
  **`claude-haiku-5-5`** for all productions. The user's decision: it's the
  better model, so no quality comparison is needed. Held or large runs are
  submitted through the **Message Batches API** (50% off). Batch latency is
  acceptable because analysis runs on request. Small automatic runs may keep
  synchronous calls.
- **Timeline review** sends every extracted event in one Opus call with no cap.
  At this scale it would overflow the context window. It must degrade
  gracefully (cap, sample, or chunk). The approach is the builder's call.

## Cost estimate (11.2 GB PST, assuming ~75k emails and ~40k attachments with text)

| Item | Estimate |
|---|---|
| Cloud Run compute for parallel chunks | ~$10–25 |
| Voyage embeddings (voyage-3) | ~$15 |
| Vision OCR (images, after skipping tiny inline ones; scanned PDF pages) | ~$10–40, depends on content |
| **Load + search total** | **~$35–80**, within the $100 limit |
| AI pipeline if requested, today's setup (Haiku 4.5) | ~$1,300+ |
| AI pipeline if requested, Haiku 5.5 + Batch API | ~$70 |

The estimate is shown on the selection screen (§2a) before any paid work. If
it exceeds $100, the screen says so before the uploader confirms.

**Neon capacity (resolved 2026-11-03).** Prod moved to the Neon project
"Vigilist AWS" (aws-us-east-2, Ohio) on the Launch plan: usage-based storage
at $0.35/GB-month, 500 GB of transfer included, compute 0.25–8 CU. Several
extra GB for a 115k-document load costs a few dollars a month, and there's no
quota cliff. The selection screen still shows a rough per-load storage
estimate alongside cost. Status: approved by the user 2026-11-03.

## Left to the builder

- Spike result and the chunk size, worker count and concurrency it implies.
- Exact `ingest_chunks` schema and migration (must be import-safe under CI's
  minimal-deps alembic run).
- How a pypff message maps onto `ParsedMessage`: headers, plain, HTML and RTF
  bodies, recipients, dates, Message-ID, attachments including embedded
  messages.
- Whether the folder path is also a column, and the tiny-image rule details.
- Synthetic PST test fixtures, from a public sample or a generator. Never
  client data.
- Timeline review's degradation strategy.

## Addendum (2026-11-03): reader = unpack once

**Measured on the real 11.2 GB PST.**
- Streaming pypff reads from Cloud Storage is unusable. Two gcs_stream runs received about 110 GB in 60 minutes and about 85 GB in 25 minutes, roughly 8–10× read amplification, and neither finished the survey. FUSE was not tested.
- Copying the file into a 32 GiB instance's memory works well:
  - download takes 207 s, at about 1× read amplification;
  - the full survey takes 104 s and finds 50,272 items (44,568 email, 966 calendar, 1,313 contact, 3,425 other);
  - reading runs at about 524 items/s;
  - RSS is 285 MiB on top of the file itself.

**The user's decision: unpack once.** This replaces §1's "chunk workers stream the PST" with the following.

- **One Cloud Run Job per large container** (`vigilist-pst-explode`).
  - It uses the same image as the backend, with 8 vCPU and 32 GiB memory, a multi-hour task timeout and 1 retry.
  - The start endpoint triggers it through the Cloud Run Admin API, in place of the survey Cloud Task.
- **What the Job does:**
  1. Download the PST into in-memory `/tmp` using a sliced download.
  2. Run `survey()` locally and write `job.survey[path]` exactly as `run_survey` does today, including the attempt/failure handling.
  3. Explode **every** item of every type to Storage, keyed by traversal position: `productions/{pid}/exploded/{job_id}/{container_key}/{folder_index:05d}/{position:07d}.json`.
     - Each item file is a serialized `ParsedMessage` plus its kind and folder path.
     - Attachments are stored as sibling objects, not inlined.
     - A per-folder manifest lists each position's kind and any read errors.
  4. Move the job on to `awaiting_selection` once every large container has been exploded.
- **Read errors are never dropped.**
  - A partially readable item is written with what could be read. An unreadable attachment becomes an error-status attachment Document.
  - A wholly unreadable item is recorded in the manifest, then reported by the chunk worker as failed, with its folder and position.
  - Embedded-message (method 5) and OLE (method 6) attachments must be read by their proper method, not `read_buffer`.
- **What stays the same:** selection, `plan_chunks` and `split_chunk` (slices remain `[folder_index, start, end)`), control numbering, finalize, the hold and the UI.
- **What changes in the chunk worker:** it reads the exploded objects for its slice in place of the PST, and builds `ParsedMessage` from JSON. Workers stay at 4 GiB, concurrency 1, 30 instances.
- **Cleanup:** the exploded prefix is deleted when the job completes, at the post chunk.
- **Cost:** about $1–2 of Job compute per mailbox. Explode time is a few minutes.

## Rollout

1. Ship behind the size threshold. Small loads are untouched.
2. Re-run the failed 11.2 GB load (a new ingest of the existing upload) and
   verify reconciliation, timing and cost against this document.
