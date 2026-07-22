# P1-4/5 — Redaction QC + Privilege Log

**Date:** 2026-07-22
**Phase:** 1 (Redaction & Privilege), sub-projects P1-4 + P1-5
**Depends on:** P1-1 (Redaction model/CRUD), P1-2/3 (redacted rendition, PR #36 — merged)
**Consumed by:** Phase 2 (production output blocks unapproved redacted docs; log export formats PDF/DAT)

## Decision context (brainstormed 2026-07-22)

- **Privilege source of truth = flagged tags.** `tags.is_privilege` boolean; the
  user marks which tags (e.g. "Attorney-Client", "Work Product") carry
  privilege. Log basis derives from privilege tag names plus, for
  redact-in-part docs, redaction reason codes. No parallel designation
  workflow.
- **Disposition = derived + override.** Default rule: privilege tag +
  redactions ⇒ `redact_in_part`; privilege tag + no redactions ⇒ `withhold`.
  Nullable per-doc override (`withhold` | `redact_in_part` | `produce`) for
  the exceptions.
- **Log descriptions = deterministic template + manual override.** Template
  built ONLY from safe typed metadata (author/recipients/date/basis) — no AI
  in the loop, no privileged substance in the log. Per-doc manual description
  field wins when set.
- **QC = per-doc, every redacted doc, auto-invalidating.** Append-only
  decisions; current status is COMPUTED: any redaction change after the
  latest decision reverts the doc to pending. Stale approvals structurally
  cannot ship. Manager+ decides; no self-QC restriction (small team).
- **Export = CSV now**; PDF and DAT ride with Phase 2's production/load-file
  machinery.

## 1. Data model (one migration, import-safe: no `app.*` imports)

- `tags.is_privilege` — Boolean, NOT NULL, server_default false.
- `documents.privilege_disposition` — String(20), nullable (override; NULL = derived).
- `documents.privilege_description` — Text, nullable (manual log description; NULL = template).
- New table `redaction_qc_decisions` (append-only):
  - `id` Integer PK autoincrement
  - `document_id` UUID FK documents.id ondelete CASCADE, indexed
  - `decision` String(20) NOT NULL — `approved` | `rejected`
  - `note` Text nullable
  - `decided_by` String(128) NOT NULL
  - `decided_at` DateTime server_default now() NOT NULL

No columns store QC status or effective disposition — both are computed.

## 2. Pure service — `app/services/privilege.py` (no DB/network)

```python
DISPOSITIONS = frozenset({"withhold", "redact_in_part", "produce"})

def effective_disposition(has_privilege_tag: bool, has_redactions: bool,
                          override: str | None) -> str | None
# override (validated against DISPOSITIONS) wins; else privilege+redactions ->
# "redact_in_part"; privilege alone -> "withhold"; redactions alone ->
# "redact_in_part" (non-privilege redactions still must be produced redacted);
# neither -> None (ordinary produce, not a log row unless override says so).

def qc_status(redaction_count: int,
              latest_decision: tuple[str, datetime, int] | None,
              latest_redaction_change_at: datetime | None) -> str
# latest_decision = (decision, decided_at, redaction_count_at_decision).
# redaction_count == 0 -> "not_applicable".
# no decision -> "pending".
# auto-invalidation -> "pending" when EITHER a redaction changed at/after
#   decided_at (edits/additions) OR current redaction_count differs from the
#   snapshot (deletions).
# else the decision value ("approved" / "rejected").

def log_description(email_from: str | None, email_to: str | None,
                    date_sent: datetime | None, file_type: str | None,
                    basis: list[str], manual: str | None) -> str
# manual wins verbatim. Else deterministic template, e.g.:
# "Email from {from} to {to} dated {YYYY-MM-DD} withheld/redacted on the basis
#  of {basis, comma-joined}" — degrade gracefully when fields are None
# ("Document dated ...", omit clauses). NEVER includes text_content, summary,
# or title (title can be AI-derived from content).
```

`latest_redaction_change_at` = max over the doc's CURRENT redactions of
`updated_at or created_at`. The timestamp rule alone misses deletions (a
delete removes rows without bumping any timestamp), so each decision row
snapshots `redaction_count` (Integer NOT NULL — add this column to the
`redaction_qc_decisions` table above): a count mismatch also reverts to
pending. Together the two checks cover add, edit, and delete after approval.

## 3. Endpoints

- `PUT /api/tags/{tag_id}` (tags router) — body `{is_privilege: bool}`,
  manager+ on the tag's production. Audit-logged (`tag_privilege_set`).
- `GET /api/productions/{production_id}/redaction-qc` — QC queue: all docs in
  the production having ≥1 redaction, each with: doc id, bates, redaction
  count, computed `qc_status`, latest decision (decision/note/by/at) if any.
  Any role with production access may read.
- `POST /api/documents/{doc_id}/redaction-qc` — body
  `{decision: "approved"|"rejected", note?: str}`; manager+; 422 if the doc
  has no redactions; appends a decision row snapshotting current
  redaction_count; audit-logged (`redaction_qc_decided`).
- `PUT /api/documents/{doc_id}/privilege` — body
  `{disposition?: str|null, description?: str|null}` (explicit null clears an
  override); manager+; disposition validated against DISPOSITIONS;
  audit-logged (`privilege_override_set`).
- `GET /api/productions/{production_id}/privilege-log` — JSON rows for every
  doc whose effective disposition is `withhold` or `redact_in_part`:
  bates_begin/end, date_sent (fallback date_received), custodian, email_from,
  email_to, file_type, disposition, basis (privilege tag names +
  reason-code labels for docs with redactions, deduped, sorted),
  description (template or manual), qc_status.
- `GET /api/export/privilege-log/csv?production_id=` — same rows as CSV
  (export.py pattern), filename `privilege_log.csv`. Header row:
  Bates Begin, Bates End, Date, Custodian, Author, Recipients, Doc Type,
  Disposition, Privilege Basis, Description, Redaction QC.

Basis labels for reason codes reuse `REASON_LABELS` from
`app/services/redaction_render.py` (single source, already sync-tested
against `REDACTION_REASON_CODES`).

## 4. Testing

TDD throughout. Pure tests for `privilege.py` (disposition matrix, qc_status
freshness incl. edit-after-approve and delete-after-approve, description
templates incl. missing-field degradation and manual override). Fake-session
endpoint tests (pattern of tests/test_redacted_rendition.py) for: role
gates, 422 no-redactions QC, append-only decisions, queue status computation,
log row assembly (privilege-only doc → withhold; privilege+redactions →
redact_in_part; redactions-only → redact_in_part with reason-code basis;
override wins; produce-override excluded from log), CSV shape.

## Out of scope (explicit)

- PDF/DAT log export; blocking production on QC (both Phase 2).
- QC UI, redaction-draw UI (redesign branch).
- AI-drafted descriptions.
- Backfill of historical audit data; no changes to existing QCDecision
  (review QC) machinery.
