# P3-1 — Search-Term Hit Reports

**Date:** 2026-07-23
**Phase:** 3 (Defensibility & Analytics), sub-project 1 of 4
**Depends on:** existing FTS (`build_tsquery`, `text_search_vector`), Phase-0 families, P0-SP5 source filter
**Consumed by:** meet-and-confer / negotiated term lists; P3-2..4 are independent.

## Decision context (approved 2026-07-23)

Standard search-term-report semantics: per term — documents with hits,
family-expanded count (hits plus their family members), and UNIQUE hits
(documents no other term matches — the marginal-value number used to argue
a term in or out). Summary: corpus size, docs hit by any term,
family-expanded any-hit total. Reports are durable artifacts: the saved
term list persists its last run (`results` JSONB + `computed_at`) and can
be re-run; every run is audit-logged. Optional `source_type` filter runs
the report against just the received production or just our collection —
how term negotiation actually splits. Terms go through the existing
`build_tsquery` (phrases, AND/OR/NOT, wildcards all work). Backend + CSV
first; UI panel is a small follow-up like the builder options.

## 1. Data model (one migration `a3b4c5d6e7f8`, down_revision `f2a3b4c5d6e7`)

`search_term_reports`: `id` int PK; `production_id` FK productions CASCADE,
indexed; `name` String(255); `terms` JSONB (list of strings); `results`
JSONB nullable (last run); `computed_at` DateTime nullable; `created_by`
String(128); `created_at` server_default now(). (Heads note: open PR #48
carries another migration off an older head — whichever merges second
needs a bump/merge migration.)

## 2. Service — `app/services/search_terms.py`

`run_search_term_report(db, production_id, terms, source_type=None) -> dict`:
per term one FTS id+family query (skipped/zero when `build_tsquery`
sanitizes to empty); ONE family map query (`id, family_id WHERE family_id
IS NOT NULL` under the same scope) and ONE corpus count; family expansion
and uniqueness computed in Python from the id sets. Result shape:

```json
{"total_docs": N, "any_hits": n, "any_with_families": n,
 "source_type": null | "collection" | "received",
 "terms": [{"term", "hits", "with_families", "unique_hits"}],
 "computed_at": iso}
```

`source_type` semantics mirror search: `received` exact,
`collection` = NOT received (NULL counts as ours).

## 3. Endpoints — new `app/routers/search_terms.py` (`/api`), registered in main

- `POST /productions/{pid}/search-term-reports` `{name, terms}` — manager+;
  422 empty name / empty terms / >200 terms / blank entries. → Out.
- `GET /productions/{pid}/search-term-reports` — list (any role w/ access).
- `GET /search-term-reports/{id}` — detail incl. stored results.
- `POST /search-term-reports/{id}/run` `{source_type?}` — manager+;
  computes, persists results+computed_at, audits (`search_term_report_run`),
  returns results.
- `DELETE /search-term-reports/{id}` — manager+.
- `GET /search-term-reports/{id}/csv` — last run as CSV
  (`Term,Documents with hits,Docs + families,Unique hits` + TOTAL row);
  404 if never run.

## 4. Testing

Service: fake-session with callable responders (per-term queries share one
substring — a queue-popping callable serves them in order); uniqueness,
family expansion incl. non-hit family members, empty-tsquery term → zeros,
source filter passes through. Endpoints: role gates, validation 422s, run
persists + audits, CSV shape, csv-before-run 404. Migration purity/head;
full suite green.

## Out of scope

- UI panel (follow-up); scheduling/recurring runs; per-custodian breakdowns
  (add columns later if requested); cross-matter reports.
