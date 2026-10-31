# Email-address search + bulk privilege coding — design

Date: 2026-10-30
Status: approved in brainstorming; awaiting spec review

## Problem

We have an email production and need to mark everything from (or involving) a
given person as privileged in one action. Two gaps block this today:

1. **Search can't filter on email participants.** `email_from`, `email_to`,
   `email_cc`, `email_bcc` are typed columns on `Document` (models.py:160-164),
   but `/api/search` metadata filters only match the `metadata_` JSONB. Native
   emails are only findable by sender via full-text.
2. **Bulk actions only cover checked rows on the visible page.** There is no
   way to act on "every hit of this search", and no bulk privilege action at
   all (the per-doc `PUT /documents/{id}/privilege` has no UI client).

## Goals / success criteria

- A reviewer can search `jane@lawfirm.com` (or `@lawfirm.com`) and choose
  **Sender only** or **Any participant** (from/to/cc/bcc).
- From the results, they can mark **all N matching documents** privileged by
  applying a privilege tag, with a confirm step showing the exact count.
- Optionally (manager+), the same action sets the per-document privilege
  override: disposition and/or a log description applied to the whole set.
- Attachments of matched emails are included by default (implemented via `family_id`; the UI says "attachments", never "family").
- Privilege log and production withholding pick this up with no changes to
  `privilege.py` / `privilege_log.py`.

## Non-goals

- Email filter in review queues/batches (`batching.py`) — follow-up.
- Bulk tag removal / bulk un-privilege — follow-up.
- Normalising participants into a separate address table.

## Background: tag vs. override

`effective_disposition` (services/privilege.py:19): override wins → else
redactions ⇒ `redact_in_part` → else privilege tag ⇒ `withhold`.

- **Privilege tag** (`Tag.is_privilege`) — says *it's privileged and why*. Sets
  the log **basis** (tag name) and implies `withhold`; log description is
  auto-generated from safe metadata.
- **Override** (`privilege_disposition`, `privilege_description`) — says *what
  to do differently*: force `withhold` / `redact_in_part` / `produce`, and/or
  replace the generated log description verbatim. Does not set basis.

The bulk action always applies a tag; the override is an optional extra.

## Design

### 1. Search: email participant filter

`GET /api/search` gains:

- `email_address: str | None` — trimmed; empty ⇒ ignored.
- `email_scope: "from" | "any"` (default `"from"`).

`search_documents` gains matching kwargs. Condition is a case-insensitive
substring match (`ILIKE '%<escaped>%'`) on `email_from` for `from`, or an `OR`
over `email_from/to/cc/bcc` for `any`. LIKE metacharacters (`%`, `_`, `\`) in
the input are escaped so input is literal. This matches display-name forms
(`"Jane Doe" <jane@lawfirm.com>`) and bare domains (`@lawfirm.com`).

`email_address` counts as an active filter: it alone is enough to return
results (no `q` required), and it disables the semantic path like the other
filters.

The filter-building logic in `search_documents` is factored into a reusable
`build_search_conditions(...)` helper so the bulk endpoint resolves *exactly*
the same document set as the search the user is looking at.

No index in v1 (ILIKE substring can't use btree; per-production scans are
fine at current sizes). A `pg_trgm` GIN index is the follow-up if needed.

### 2. Bulk privilege endpoint

`POST /api/documents/bulk-privilege`

```jsonc
{
  // exactly one of:
  "doc_ids": ["uuid", ...],
  "query": {            // same fields as GET /api/search filters
    "q": "", "production_id": 12, "metadata": {...}, "file_type": null,
    "source_party": null, "source_type": null,
    "email_address": "jane@lawfirm.com", "email_scope": "any"
  },

  "tag_id": 7,                    // required; must be Tag.is_privilege
  "include_attachments": true,    // default true; expands by family_id
  "disposition": null,            // optional: withhold|redact_in_part|produce
  "description": null,            // optional log description
  "dry_run": false
}
```

Behaviour:

1. Resolve doc IDs: `doc_ids` as given, or run `build_search_conditions` (full
   result set, no paging, scoped to accessible productions; `query` must carry
   at least one filter or text, else 422).
2. If `include_attachments`, expand to all documents sharing a non-null
   `family_id` **within the same production**.
3. Authorize **per production** touched: readonly ⇒ 403; if `disposition` or
   `description` set, require manager+ (matches the single-doc endpoint).
   (Note: existing `bulk-tag` checks only the first doc's production — we don't
   copy that.)
4. Validate `tag_id` exists and `is_privilege` (422 otherwise); validate
   `disposition` ∈ `DISPOSITIONS`.
5. `dry_run` ⇒ return counts only, no writes:
   `{matched, attachments_added, total, already_tagged}`.
6. Otherwise: insert missing `DocumentTag` rows set-wise (one query for
   existing, `insert` of the rest — no per-doc round trips); if override
   fields provided, one `UPDATE ... WHERE id IN (...)` setting only the fields
   provided. One `log_action("bulk_privilege_applied", ...)` per production
   with tag, counts, criteria and override fields (not the full ID list for
   query-mode). Single commit.
   Returns `{total, tagged, already_tagged, overridden}`.

Hard cap: 50,000 docs per call (422 above that, telling user to narrow).

New code lives in `routers/privilege.py` (endpoint) and a small
`services/bulk_privilege.py` (resolve + family expansion + apply) so it's
testable without HTTP.

### 3. Frontend

- **Omnibox** (`components/Omnibox.tsx`): an "Email address" input with a
  two-state toggle **Sender only / Any participant**. Wired through
  `searchDocuments` (`api/client.ts`) as `email_address` / `email_scope`.
- **Bulk bar** (`App.tsx`): when a search is active and every visible row is
  selected, show "Select all N matching" (Gmail pattern). Selection then
  becomes "query mode" (the current search criteria) instead of an ID list;
  clearing or changing the search exits query mode.
- **"Mark privileged…" button** in the bulk bar opens a dialog:
  - Privilege tag picker (only `is_privilege` tags; option to create one).
  - "Include attachments" checkbox (on). User-facing copy says "attachments" throughout.
  - Collapsed **Advanced (manager)** section: disposition select (blank =
    no override) and description textarea. Hidden for non-managers.
  - On open/changes, calls the endpoint with `dry_run: true` and shows
    "Apply to **1,284** documents (1,102 matched + 182 attachments; 40 already
    tagged)". Confirm button performs the real call, then refreshes results.
- New client functions: `bulkPrivilege(body)`.

## Error handling

- 422: no criteria, non-privilege tag, bad disposition, over cap.
- 403: inaccessible production, readonly, override without manager.
- The dry-run count and the apply can differ if documents change between
  them; the apply response's `total` is what's shown in the success toast.

## Testing

Backend (pytest, existing DB-backed test setup):

- Email filter: `from` vs `any`; display-name format; domain match;
  case-insensitivity; LIKE metacharacters treated literally; email filter alone
  returns results; combined with `q` and metadata.
- Bulk endpoint: dry-run returns counts and writes nothing; query-mode set
  equals search result set; family expansion (incl. not crossing
  productions); idempotent re-apply (`already_tagged`); non-privilege tag 422;
  readonly 403; override as reviewer 403, as manager sets fields and
  `effective_disposition`/privilege log reflect it; per-production role check
  across a multi-production query.

Frontend: no test suite exists; verify manually in the local app (search by
sender and any-participant, select-all-matching, dry-run count, apply, log
reflects it).
