# P3-3 TAR Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Note:** executed inline by the planning session against the spec's exact contracts.

**Goal:** Persisted, audited TAR validation reports: control-set recall/precision with Wilson CIs and elusion testing over the null set.

**Spec:** `docs/superpowers/specs/2026-07-23-p3-3-tar-validation-design.md`

## Global Constraints
- Branch `feat/p3-3-tar-validation` stacked on `feat/p3-2-sampling-foundation` (PR base = that branch until #59 merges). Migration `d6e7f8a9b0c1` on `c5d6e7f8a9b0`; purity; re-verify heads + no force-push before PR.
- Machine mapping exactly: positive relevant|key_document, negative not_relevant, else undecided. Uncoded/conflicted/undecided excluded from the matrix and reported. Zero denominators -> null metric + note.
- Writes manager+ & audited; fake-session tests, 0 warnings; no attribution trailers.

### Task 1: Migration + model — `tar_validation_reports` per spec §1. Commit.
### Task 2: Sampling extension — `SampleCreate.scope/project_id`, `elusion` purpose, machine_negative subquery + 422; tests in test_sampling_endpoints.py. Commit.
### Task 3: Service — `tar_validation.build_validation` per spec §3 with queued-responder tests (full scenario, zero-denominators, elusion extrapolation, null elusion). Commit.
### Task 4: Router `tar.py` + schemas (`TarValidationCreate`, `TarValidationOut`) + registration + endpoint tests (422 matrix, persist + audit, role gates). Commit.
### Task 5: Full suite; head check; push; PR (base p3-2 branch).
