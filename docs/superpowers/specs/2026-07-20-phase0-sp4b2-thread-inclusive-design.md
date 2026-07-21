# Phase 0 · Sub-project 4b-2 — Email Thread + Inclusive Derivation

**Date:** 2026-07-20
**Status:** Approved design, pending implementation plan
**Roadmap:** `docs/ediscovery-parity-roadmap.md` (Phase 0, SP4 — SP4b decomposed into 4b-1 email→Documents+family+PST, and 4b-2 thread/inclusive derivation). SP4b-2 completes Phase 0.
**Branch:** `feat/phase0-sp4b2-thread-inclusive`
**Builds on:** SP1 (typed columns + backfill pattern), SP3 (`thread_id`/`is_inclusive` columns + Family/Thread panel + `propagate_tag` thread branch), SP4b-1 (email parsing → parent/child Documents).

## Summary

Derive `thread_id` (conversation grouping) and `is_inclusive` (most-complete message) for
**parsed** emails, as a production-wide post-pass. Today those two `Document` columns are only
populated when a Relativity load file supplies them (SP3); emails ingested through the SP4b-1
native/PST path leave them null/`False`. SP4b-2 fills them by (1) capturing the RFC threading
headers during parsing and (2) running a derivation engine over a production's email Documents.

Threading uses reply-chain headers (Message-ID / In-Reply-To / References, JWZ-style) with a
normalized-subject fallback. `is_inclusive` marks the leaves of each thread's reply graph (a
message no later message replies to), falling back to "latest by date" for subject-only threads.

## Current state (verified)

- `Document` has `thread_id` (String 255, nullable), `is_inclusive` (Boolean, not-null default
  `False`), `family_id`, `email_from/to/cc/bcc/subject`, `date_sent` — `models.py:113-134`.
- SP4b-1 `services/email_parse.py` `@dataclass ParsedMessage {from_, to, cc, bcc, subject,
  date_sent, body_text, attachments}` — does **not** capture Message-ID / In-Reply-To /
  References. `_parse_eml_bytes` (stdlib) and `_parse_msg_bytes` (extract-msg) build it.
- SP4b-1 `services/ingest_native.py` `build_email_documents(...)` builds the parent email
  Document + child attachment Documents; `process_native_email` calls it; `ingest_native_batch`
  persists the family atomically (`_persist_documents`).
- `routers/intelligence.py`: `GET /documents/{id}/family` returns `thread` = docs sharing
  `thread_id`; `propagate_tag` `"thread"` branch tags docs sharing `thread_id`. Both already
  consume the column — SP4b-2 just populates it.
- `services/ingest.py` `_finalize_job_if_done(db, job, production_id, errors)` runs once a job's
  files are all accounted for (AI titles, mark complete). This is the post-ingest hook point.
- Alembic single head `p7c2d4e06f13`; deploy runs `alembic upgrade head` then ships. SP3's
  `n6b1c3d95e02` is the idempotent, per-batch-committed **data-backfill** migration to mirror.
- `field_mapping.py`/`metadata_normalize.py` already map `thread_id`/`is_inclusive` from load
  files (SP3) — unchanged here; SP4b-2 is the *derivation* path for parsed email.

## Design

### 1. Capture threading headers (`email_parse.py` + schema)

- `ParsedMessage` gains three fields: `message_id: str = ""`, `in_reply_to: str = ""`,
  `references: str = ""` (References normalized to a single space-joined string of ids).
- `_parse_eml_bytes`: read `Message-ID`, `In-Reply-To`, `References` headers via the existing
  `_header(msg, name)` helper; collapse References whitespace to single spaces.
- `_parse_msg_bytes`: `message_id` from `getattr(msg, "messageId", "")`; `in_reply_to` /
  `references` from the transport headers exposed by extract-msg's `msg.header` (an
  `email.message.Message`), guarded with `getattr`/`None` checks so a `.msg` lacking them yields
  `""`. Never raises (the SP4b-1 never-raise contract is unchanged).
- **Migration (schema):** add three nullable `Document` columns — `message_id` (String 500,
  indexed for the derivation lookup), `in_reply_to` (String 500), `email_references` (Text;
  `references` is a SQL reserved word, so the column is `email_references`). `down_revision =
  p7c2d4e06f13`.
- `build_email_documents` (`ingest_native.py`): store `message_id` / `in_reply_to` /
  `email_references` on the **parent** email Document only (attachments leave them null). No
  change to control numbers, hashing, or family linking.

### 2. Derivation engine — `services/email_threading.py` (new)

A **pure, DB-free core** (so it is unit-testable and reusable by both the async service and the
sync backfill migration):

- `normalize_subject(subject: str) -> str` — lowercase, strip leading `re:`/`fwd:`/`fw:`
  prefixes (repeatedly) and surrounding whitespace; collapse internal whitespace.
- `compute_thread_assignments(messages: list[ThreadMsg]) -> dict[doc_id, ThreadAssignment]`
  where `ThreadMsg` is a small value object `{doc_id, message_id, in_reply_to, references,
  subject, date_sent}` and `ThreadAssignment` is `{thread_id: str, is_inclusive: bool}`:
  - **Thread grouping (JWZ-lite, two pass):**
    1. Build an index of `message_id -> doc`. Union-find over reply links: for each message,
       union it with the doc whose `message_id` equals its `in_reply_to` and with each id in its
       `references` that resolves to a known `message_id`. Each connected component is a thread.
    2. Messages with no `message_id` and no resolvable link are grouped by `normalize_subject`
       into subject-keyed threads (a message with links stays in its header component even if it
       shares a subject).
  - **thread_id (deterministic + stable across re-runs, order-independent):** for a
    header-formed thread, canonical key = the lexicographically-smallest `message_id` among
    members; for a subject-formed thread, canonical key = `f"subj:{normalized_subject}"`.
    `thread_id = "T-" + sha1(f"{production_id}|{canonical_key}").hexdigest()[:16]`.
    (production_id is passed into `compute_thread_assignments` so ids never collide across
    productions and are reproducible.)
  - **is_inclusive (reply-tree leaves):** within a thread, a message is inclusive **iff no other
    message in the thread replies to it** — i.e., it is a leaf of the reply graph (no member's
    `in_reply_to`/`references` resolves to this message's `message_id`). Singletons (no links)
    are leaves → inclusive. **Fallback:** for a thread that has **no reply links at all** (a
    pure subject-fallback group), mark only the single most-recent message by `date_sent`
    inclusive (ties broken by smallest `doc_id` for determinism); if all `date_sent` are null,
    the smallest `doc_id`.
- `async def derive_threads(db, production_id) -> ThreadStats`: select the production's email
  Documents (`file_type == "email"`, the marker SP4b-1 sets on parsed parent messages — so the
  derivation only ever writes `thread_id`/`is_inclusive` for parsed email and never overwrites
  values SP3 supplied from a Relativity load file on other document types) with the fields above;
  run `compute_thread_assignments`;
  `UPDATE documents SET thread_id=:t, is_inclusive=:i WHERE id=:id` for each (committed in
  batches). Idempotent — deterministic keys mean a re-run reproduces the same values. Returns
  `{threads: int, inclusive: int, messages: int}`. Never raises out to its callers (best-effort;
  logs + returns zeroed stats on failure).

Only parent email Documents carry `thread_id` (attachments remain reachable via `family_id`).

### 3. Triggers

- **Automatic:** `_finalize_job_if_done` calls `await derive_threads(db, production_id)`
  best-effort (wrapped so a threading failure never fails or un-completes the job) after the job
  is marked complete. Runs for every finalized job; a production with no email Documents is a
  cheap no-op.
- **On-demand:** `POST /productions/{id}/rethread` (in `routers/intelligence.py`, access-scoped
  via `get_accessible_production_ids`) → `derive_threads` → returns `ThreadStats`. Idempotent;
  lets a user re-run after a correction or a partial ingest.
- **Backfill (data migration):** an idempotent, per-batch-committed Alembic data-migration
  (mirroring SP3's `n6b1c3d95e02`) that, per production, loads existing email Documents and
  applies `compute_thread_assignments`, then `UPDATE`s `thread_id`/`is_inclusive`. Existing docs
  predate header capture, so `message_id`/`in_reply_to`/`email_references` are null → they thread
  via the subject fallback (expected). Reuses the pure function (imported into the migration) so
  the graph logic is not duplicated. `down_revision` = the SP4b-2 schema migration. This is the
  one prod-touching data step; verify upgrade + downgrade against real Postgres before merge.

### 4. Data flow

Parse (`email_parse`) captures headers → `build_email_documents` stores them on the parent →
ingest finalizes → `derive_threads` groups the production's emails and writes `thread_id` +
`is_inclusive` → SP3's Family/Thread panel and `propagate_tag` light up automatically (no UI
change needed; they already read the columns).

### 5. Testing

Deterministic unit tests (no DB/network), `backend/tests/`:
- `normalize_subject`: `Re:`/`FW:`/`Fwd:` (and stacked `Re: Re:`) stripped; internal text kept.
- `compute_thread_assignments`:
  - Reply chain A→B→C via `in_reply_to`: one thread; only C inclusive.
  - Branch A→B, A→C: one thread {A,B,C}; B and C inclusive, A not.
  - References-only linking (no `in_reply_to`): still one thread.
  - Subject fallback (two msgs, same normalized subject, no headers): one thread; latest by
    `date_sent` inclusive.
  - Singleton (no links, unique subject): own thread, inclusive.
  - **Determinism:** shuffling the input list yields identical `thread_id`s and inclusive set.
  - Cross-production isolation: same `message_id` in two productions → different `thread_id`.
- `email_parse`: an `.eml` with `Message-ID`/`In-Reply-To`/`References` → fields populated;
  References whitespace collapsed; a `.eml` without them → empty strings.
- The DB-bound `derive_threads`, the endpoint, and the migration are thin wrappers over the pure
  function (verified by the app + the migration's own upgrade/downgrade check); the pure engine
  carries the unit tests.

## Out of scope (SP4b-2)
- Cross-production threading (threads are scoped to one production).
- Giving attachments their own `thread_id` (they stay reachable via `family_id`).
- A dedicated date-ordered conversation view / inclusive-only review filter (deferred, as SP3).
- Re-parsing already-ingested `.pst`/`.eml`/`.msg` to backfill headers on old Documents (a
  re-ingest does that; the backfill threads old docs by subject).
- Changes to the SP3 load-file mapping path for `thread_id`/`is_inclusive`.
