# P3-2 Sampling Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Note:** executed inline by the planning session against the spec's exact contracts.

**Goal:** Dependency-free defensible-sampling math + frozen random samples + estimate/acceptance endpoints.

**Spec:** `docs/superpowers/specs/2026-07-23-p3-2-sampling-foundation-design.md`

## Global Constraints

- Branch `feat/p3-2-sampling-foundation`. Migration `c5d6e7f8a9b0`, `down_revision = "a3b4c5d6e7f8"` (verified single head at branch time — re-verify before PR; parallel sessions are active).
- Stats formulas exactly per spec §1 (Wilson; normal-approx sample size with FPC; acceptance via Wilson upper bound). Z table only for 90/95/99.
- Samples are frozen: estimates/acceptance read `document_ids` intersected with tag membership; never re-query the corpus.
- Writes manager+ & audited; tests fake-session, 0 warnings; no attribution trailers.

### Task 1: Stats module + pure tests
- [ ] `backend/app/services/sampling_stats.py` (`Z`, `sample_size`, `wilson_ci`, `acceptance`); `backend/tests/test_sampling_stats.py` covering spec §4 pure cases. Commit.

### Task 2: Migration + model
- [ ] `c5d6e7f8a9b0_add_samples.py` + `Sample` model (fields per spec §2, after `SearchTermReport`); compile/import/purity/single-head checks. Commit.

### Task 3: Schemas + router + endpoint tests
- [ ] Schemas `SampleCreate`, `SampleOut` (list view omits `document_ids`, exposes `size`); router `backend/app/routers/sampling.py` with the seven endpoints per spec §3 (draw query: scoped `select(Document.id).order_by(func.random()).limit(n)`; estimate tag query: `select(DocumentTag.document_id).where(tag_id == ..., document_id.in_(sample ids))`); register in main.
- [ ] Endpoint tests in `backend/tests/test_sampling_endpoints.py`: calculator 200 + 422s, draw computes size from params when omitted + freezes ids (responders: `"random"` for the draw, `"count"` for population), estimate intersection math, acceptance verdict, reviewer 403s, delete audit. Commit.

### Task 4: Verify + PR
- [ ] Full suite; head re-check; push; PR to main (reland-aware: verify no force-push happened first — `git fetch && git status -sb`).
