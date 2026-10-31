# Email-address search + bulk tagging by query — design

Date: 2026-10-30
Status: approved in brainstorming (rev 2: generalized from privilege-only to
any tag, e.g. Not Responsive; reviewer chooses how to handle conflicting
responsiveness tags)

## Problem

We have an email production and need to code everything from (or involving) a
given person in one action — e.g. mark all of outside counsel's emails
**privileged**, or all of a vendor's newsletters **Not Responsive**. Two gaps
block this today:

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
- From the results, they can apply **any tag** (Responsive, Not Responsive, a
  privilege tag, …) to **all N matching documents**, with a confirm step
  showing the exact count.
- When the chosen tag is a responsiveness tag and some documents already carry
  a *different* responsiveness tag, the confirm step asks the reviewer to
  **switch** those documents or **leave them as they are**. No silent default.
- When the chosen tag is a privilege tag, optionally (manager+) the same action
  sets the per-document privilege override: disposition and/or a log
  description applied to the whole set.
- Attachments of matched emails are included by default (implemented via
  `family_id`; the UI says "attachments", never "family").
- Privilege log, production withholding, TAR validation and exports pick this
  up with no changes — it writes ordinary `DocumentTag` rows.

## Non-goals

- Email filter in review queues/batches (`batching.py`) — follow-up.
- Bulk tag removal (other than the conflict switch) — follow-up.
- Exclusivity rules for categories other than `responsiveness`.
- Normalising participants into a separate address table.

## Background

### Tags involved

- Responsiveness tags are seeded with `category="responsiveness"`:
  "Responsive", "Not Responsive", "Needs Review" (services/review_tags.py:15).
  AI-review accept writes these same tags.
- Privilege tags have `Tag.is_privilege=True`.

### Privilege tag vs. override

`effective_disposition` (services/privilege.py:19): override wins → else
redactions ⇒ `redact_in_part` → else privilege tag ⇒ `withhold`.

- **Privilege tag** — says *it's privileged and why*. Sets the log **basis**
  (tag name) and implies `withhold`; log description is auto-generated from
  safe metadata.
- **Override** (`privilege_disposition`, `privilege_description`) — says *what
  to do differently*: force `withhold` / `redact_in_part` / `produce`, and/or
  replace the generated log description verbatim. Does not set basis.

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

### 2. Bulk tag-by-query endpoint

`POST /api/documents/bulk-tag-by-query`

```jsonc
{
  // exactly one of:
  "doc_ids": ["uuid", ...],
  "query": {            // same filter fields as GET /api/search
    "q": "", "production_id": 12, "metadata": {...}, "file_type": null,
    "source_party": null, "source_type": null,
    "email_address": "jane@lawfirm.com", "email_scope": "any"
  },

  "tag_id": 7,                    // required; any tag
  "include_attachments": true,    // default true; expands by family_id
  "conflict_action": null,        // "switch" | "leave"; required if conflicts > 0
  "disposition": null,            // privilege tags only: withhold|redact_in_part|produce
  "description": null,            // privilege tags only: log description
  "dry_run": false
}
```

Behaviour:

1. Resolve doc IDs: `doc_ids` as given, or run `build_search_conditions` (full
   result set, no paging, scoped to accessible productions; `query` must carry
   at least one filter or text, else 422).
2. If `include_attachments`, expand to all documents sharing a non-null
   `family_id` **within the same production**.
3. Validate: tag exists (404); `disposition`/`description` given on a
   non-privilege tag ⇒ 422; `disposition` ∉ `DISPOSITIONS` ⇒ 422.
4. Authorize **per production** touched: readonly ⇒ 403; override fields set ⇒
   manager+ required (matches the single-doc endpoint). (Existing `bulk-tag`
   checks only the first doc's production — we don't copy that.)
5. **Conflicts:** if the tag's `category == "responsiveness"`, a conflict is a
   target doc that has another tag with category `responsiveness` and doesn't
   already have the chosen tag. (Exclusive categories are a module constant
   `EXCLUSIVE_CATEGORIES = {"responsiveness"}`.)
6. `dry_run` ⇒ no writes; return
   `{matched, attachments_added, total, already_tagged, conflicts,
   conflict_tags: [{tag_id, name, count}]}`.
7. Apply (not dry run): if `conflicts > 0` and `conflict_action` is null ⇒ 409
   with the dry-run payload (so the UI can ask). Then:
   - `switch`: delete the conflicting same-category `DocumentTag` rows on
     those docs, then tag them.
   - `leave`: drop conflicting docs from the target set (they keep their
     current tag and get neither the new tag nor the override).
   - Insert missing `DocumentTag` rows set-wise (one select for existing, one
     bulk insert — no per-doc round trips).
   - If override fields were provided, one `UPDATE ... WHERE id IN (...)`
     setting only the provided fields.
   - One `log_action("bulk_tag_by_query", ...)` per production with tag,
     counts, conflict_action, criteria and override fields (not the full ID
     list in query mode). Single commit.
   - Returns `{total, tagged, already_tagged, switched, left, overridden}`.

Hard cap: 50,000 docs per call (422 above that, telling user to narrow).

Code: endpoint in `routers/tags.py` next to `bulk-tag`; logic in a new
`services/bulk_tagging.py` (resolve, attachment expansion, conflict detection,
apply) so it's testable without HTTP.

### 3. Frontend

- **Omnibox** (`components/Omnibox.tsx`): an "Email address" input with a
  two-state toggle **Sender only / Any participant**. Wired through
  `searchDocuments` (`api/client.ts`) as `email_address` / `email_scope`.
- **Bulk bar** (`App.tsx`): when a search is active and every visible row is
  selected, show "Select all N matching" (Gmail pattern). Selection then
  becomes "query mode" (the current search criteria) instead of an ID list;
  clearing or changing the search exits query mode.
- **"Tag all matching…" button** in the bulk bar opens a dialog:
  - Tag picker, with Responsive / Not Responsive / privilege tags first.
  - "Include attachments" checkbox (on).
  - When a privilege tag is picked: collapsed **Privilege log (manager)**
    section with disposition select (blank = no override) and description
    textarea. Hidden for non-managers and for non-privilege tags.
  - On open/changes, calls the endpoint with `dry_run: true` and shows
    "Apply to **1,284** documents (1,102 matched + 182 attachments; 40 already
    tagged)".
  - If `conflicts > 0`: "**37** of these are currently tagged *Responsive*."
    with a required radio — **Switch them to Not Responsive** / **Leave them as
    they are** — the Apply button is disabled until one is chosen.
  - Apply performs the real call, shows a toast from the response, refreshes
    results. A 409 (conflicts appeared since the dry run) re-renders the
    dialog with the new counts.
- New client function: `bulkTagByQuery(body)`.

## Error handling

- 404: unknown tag. 409: conflicts present without `conflict_action`.
- 422: no criteria, override fields on non-privilege tag, bad disposition,
  over cap.
- 403: inaccessible production, readonly, override without manager.
- The dry-run count and the apply can differ if documents change between
  them; the apply response is what's shown in the success toast.

## Testing

Backend (pytest):

- Email filter: `from` vs `any`; display-name format; domain match;
  case-insensitivity; LIKE metacharacters treated literally; email filter alone
  returns results; combined with `q` and metadata.
- Bulk endpoint/service: dry-run returns counts and writes nothing; query-mode
  set equals search result set; attachment (family_id) expansion incl. not
  crossing productions; idempotent re-apply (`already_tagged`); conflicts
  detected only for responsiveness tags; 409 without `conflict_action`;
  `switch` removes the old responsiveness tag; `leave` skips those docs;
  override on non-privilege tag 422; readonly 403; override as reviewer 403,
  as manager sets fields; per-production role check across a multi-production
  query.

Frontend: no test suite exists; verify manually in the local app (search by
sender and any-participant, select-all-matching, dry-run count, conflict
prompt both ways, apply privilege + Not Responsive, privilege log reflects it).
