# P3-4 Custody + Phase-3 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Note:** executed inline by the planning session against the spec's exact contracts.

**Goal:** Read-only lineage / exceptions / chain-of-custody endpoints (pure assembly, no migration) + the DefensibilityPanel surfacing all of Phase 3.

**Spec:** `docs/superpowers/specs/2026-07-23-p3-4-custody-and-ui-design.md`

## Global Constraints
- Branch `feat/p3-4-custody-and-ui` stacked on `feat/p3-3-tar-validation` (PR base = that branch until #60 merges). NO migration.
- All three report endpoints read-only, any role with matter access; response shapes exactly per spec §1.
- Frontend gate `npm run build`; fake-session backend tests 0 warnings; no attribution trailers.

### Task 1: `app/routers/defensibility.py` (lineage, exceptions + CSV, chain-of-custody) + registration + fake-session tests. Commit.
### Task 2: client.ts wrappers + `DefensibilityPanel.tsx` (four sections per spec §2) + App mount (all modes). Build green. Commit.
### Task 3: Full suite; fresh-main check; push; PR (base p3-3 branch).
