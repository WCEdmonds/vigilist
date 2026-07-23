# P3-3 — TAR Validation (Recall, Precision, Elusion)

**Date:** 2026-07-23
**Phase:** 3, sub-project 3 of 4 — the flagship
**Depends on:** P3-2 (Sample table + `sampling_stats`, PR #59 — stacked branch),
review projects (`ReviewProject`/`AIReviewResult`), tags
**Consumed by:** validation reports attached to declarations; P3-4 references
report ids in lineage.

## Decision context (approved 2026-07-23)

- **Validation is per review project** — `AIReviewResult.ai_decision` under a
  chosen `ReviewProject` is the classifier being validated. Machine mapping:
  positive = `relevant` | `key_document`; negative = `not_relevant`;
  undecided = `needs_review` or no result row.
- **Human truth = tags, blind to the machine.** A control set is a P3-2
  `Sample` (purpose `control`) coded with a responsive tag and (optionally)
  a non-responsive tag for completeness accounting. Docs with neither =
  uncoded; with both = conflicted. Both are REPORTED and excluded from the
  confusion matrix — visible honesty beats silent inclusion.
- **Elusion** = a P3-2 draw over the project's null set (new draw scope
  `machine_negative` + purpose `elusion`), coded with the responsive tag;
  elusion rate gets a Wilson CI and extrapolates to "estimated missed
  documents" against the null-set size.
- **Reports persist** (`tar_validation_reports`): params + results JSONB,
  audited — the artifact you attach to a declaration. Recompute = new row;
  history is the point.
- All intervals via `wilson_ci`; recall = TP/(TP+FN), precision =
  TP/(TP+FP), richness = human-positives/coded, each with CI. Zero
  denominators return null metrics with a reason string, never divide.

## 1. Data model (migration `d6e7f8a9b0c1`, down_revision `c5d6e7f8a9b0`)

`tar_validation_reports`: `id`; `production_id` FK CASCADE indexed;
`project_id` FK review_projects CASCADE; `params` JSONB; `results` JSONB;
`created_by`; `created_at`.

## 2. Sampling extension (P3-2 router, same stack)

`SampleCreate` gains `scope: str | None` (`machine_negative`) and
`project_id: int | None`; `PURPOSES` gains `elusion`. Draw with
`scope="machine_negative"` requires `project_id` (422 otherwise) and adds
`Document.id IN (SELECT document_id FROM ai_review_results WHERE project_id
= :p AND ai_decision = 'not_relevant')` to the scope. Params record scope +
project_id.

## 3. Service — `app/services/tar_validation.py`

`build_validation(db, production_id, project_id, control_sample,
responsive_tag_id, nonresponsive_tag_id, elusion_sample, confidence) -> dict`:

- Queries: machine decisions for control ids (one), responsive-tag members
  (control), nonresponsive-tag members (control), responsive-tag members
  (elusion), null-set count for the project.
- Result shape:

```json
{"confidence": 95, "project_id": N,
 "control": {"sample_id", "n", "coded", "uncoded", "conflicted",
             "machine_undecided",
             "richness": {"rate","low","high"} | null,
             "confusion": {"tp","fp","fn","tn"},
             "recall": {...} | null, "precision": {...} | null,
             "notes": [str]},
 "elusion": {"sample_id", "n", "positives", "rate","low","high",
             "null_set_size", "estimated_missed_low", "estimated_missed_high"}
            | null,
 "generated_at": iso}
```

## 4. Endpoints — new `app/routers/tar.py` (`/api`), registered in main

- `POST /productions/{pid}/tar-validation` body `{project_id,
  control_sample_id, responsive_tag_id, nonresponsive_tag_id?,
  elusion_sample_id?, confidence?}` — manager+, audited
  (`tar_validation_run`). 422: project/sample not in this production,
  control sample purpose != `control`, elusion sample purpose != `elusion`,
  bad confidence. Persists a report row; returns results.
- `GET /productions/{pid}/tar-validation` — list (any access; results
  included — they're the artifact).
- `GET /tar-validation/{id}` — detail.

## 5. Testing

Service (fake-session, queued responders for the same-substring tag
queries): full scenario (uncoded + conflicted + machine-undecided docs all
excluded and reported; hand-checked tp/fp/fn/tn and recall/precision
rates), zero-denominator paths (no human positives → recall null + note),
elusion math + extrapolation, no-elusion-sample → null. Sampling extension:
machine_negative draw adds the subquery + 422 without project_id. Endpoints:
validation 422s, persist + audit, role gates. Full suite green.

## Out of scope

- UI (Phase-3 panel follow-up after P3-4).
- Coding workflow conveniences (queue from a sample) — tags suffice.
- Attorney-decision-based truth (must stay independent of the AI surface).
