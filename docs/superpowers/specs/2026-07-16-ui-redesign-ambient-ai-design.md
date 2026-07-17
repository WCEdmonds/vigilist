# Vigilist UI Redesign + Ambient AI — Design

**Date:** 2026-07-16
**Status:** Approved by owner (brainstorm session)
**Branch context:** builds on `feat/onboarding-guide`

## Problem

Two issues, one root cause:

1. The UI works but is unpolished and hard to navigate — a 7-button header of equal-weight actions, heavy inline styling drifting from the token system, and an off-theme Corpus Analysis palette.
2. The AI features are never used. The firm actively uses Vigilist for core review (search, view, tag), but nobody touches Smart Review, Corpus Analysis, the Clusters strip, summarize, or find-similar. The AI Agent chat is too new to judge.

Root cause for (2): every AI feature is an opt-in destination (a page, a strip, a FAB, a sidebar button) that demands the user already understand what it does. None of them appear inside the three jobs reviewers actually do: **getting oriented in a new production, running a relevance pass, and hunting hot docs.**

## Decisions (made during brainstorm)

| Decision | Choice |
|---|---|
| Scope | Full visual redesign + AI workflow rework |
| AI posture | **Ambient**: AI runs at ingest; reviewers open the app and the work already exists |
| Visual direction | **Refined Archive** — keep the parchment/ink identity, execute it rigorously |
| Home layout | **Brief-first Home + right context rail** (no left-rail nav) |
| Header | **Command bar** — search/ask omnibox lives in the header |
| Case context source | Asked at ingest (one "About this case" step) |
| Cost posture | Orientation runs silently; relevance classification is cost-gated with a pre-checked estimate in the ingest wizard |

## 1. Information architecture & navigation

### Header → command bar

One 52px ink-blue bar:

```
Vigilist · [Production switcher ▾] · [Search-or-ask omnibox] · ✦ Review · ⊞ Dashboard · ⚙ · avatar
```

- **Omnibox** = existing SearchBar relocated. Keeps full-text/semantic auto-detection but makes it visible: a hint line under the box shows "Searching text" vs "Asking the production," with a manual toggle. Question-shaped input can be sent directly to AI chat (opens in the context rail).
- **Production switcher** (dropdown) replaces the ProductionPicker page for switching. The picker page remains as the multi-production landing experience (see below).
- **⚙ menu** absorbs: Share, + Ingest, Audit Log, Guide, Production settings (new; edits case context), sign-out. Owner-only items hidden for non-owners, same rules as today.
- **Avatar chip** uses a new brass/gold accent token — the single warm accent in the ink bar.

### Production picker → "Case desk" landing

Each production is a card: title, case description (from the new ingest step), doc count, review progress bar, one-line AI theme summary from its Brief, active users. Owners see pipeline status (OCR/AI progress) on the card. "+ Ingest a production" is a first-class card. Users with exactly one production skip straight to Home (unchanged behavior).

### Page inventory after the redesign

| Surface | Fate |
|---|---|
| Home | Rebuilt: Brief + list + context rail (§3) |
| TopicGroups "Clusters (beta)" strip | **Deleted** — themes live in the Brief |
| CorpusAnalysis page | **Deleted as destination** — donut/theme exploration folds into Brief expansion |
| AIReviewPage, QueueManager, BatchReview entry | **Merged** into one Review workspace (§3) |
| AI Agent FAB + `.ai-agent-*` overlay | **Deleted** — chat re-homes into the context rail |
| DocumentViewer | Kept; Summary tab reads precomputed summary; AI Tools buttons fold into metadata sidebar AI section |
| Dashboard, AuditLog, ManageAccess, IngestWizard | Kept, restyled (wizard gains steps, §2) |
| Onboarding guide | Kept; **rewritten late in the project** to describe the new UI (it currently teaches the FAB and Clusters strip) |

Navigation stays state-driven via `useUrlState` — no router library. This is a reorganization of what's mounted, not a routing rewrite.

## 2. Ambient AI pipeline

### Ingest wizard: new "About this case" step

One textarea, a few sentences: what the case is about, what makes a document relevant. Stored per production (e.g. `productions.case_context`). Editable later via ⚙ → Production settings, because criteria evolve.

### Auto-run at ingest completion (silent, no gate)

Extends the existing post-ingest chain (OCR → text → titles → embeddings):

1. **Clustering + theme labels** — existing `services/clustering.py`, now triggered automatically.
2. **Per-document summaries** — Haiku, batched like `generate_titles_batch`, written to `documents.summary`. Viewer Summary tab becomes instant.
3. **Production Brief** — new service call: case context + cluster labels + representative docs per cluster + metadata stats (date range, senders/custodians, file types) → structured JSON brief (overview paragraph, key players, date range, themes with counts, notable documents). Stored on the production row. Sonnet-class model, single call.

### Cost-gated: relevance classification

Final wizard screen: "Classify all N documents against your case description — est. $X" with a **pre-checked** box. Estimate = docs × avg extracted-text tokens × Sonnet pricing. If checked, a review project is **auto-created from the case context** (reusing `services/ai_review.py` and review-project machinery) and runs server-side after ingest.

### Progress & failure model

- Per-stage status on the production (e.g. `ai_pipeline_status` JSON: clustering / summaries / brief / classification, each pending→running→done→failed).
- Home shows a Brief skeleton ("AI is reading the production…") until ready.
- Stage failure degrades gracefully (no brief ≠ broken Home); owner sees a retry card per failed stage.
- Documents are searchable the moment text extraction lands — the AI pipeline never blocks availability.
- Cost-estimate failure blocks only the classification checkbox, never ingest.

### Retrofit

Owner-visible "Generate brief" card on Home runs the same pipeline for pre-existing productions. This is also the migration path for current data.

## 3. Home, context rail, Review workspace

### Home

- **Production Brief card** at top: serif headline, AI overview paragraph, key players, date range, theme chips with counts. Theme chip click filters the list (relocated cluster-filter mechanics). Expansion reveals the full theme breakdown (donut + per-theme key docs, ex-CorpusAnalysis). Collapsible; remembers state; collapsed form is one line with theme chips visible.
- **Document list** unchanged structurally, restyled, plus two AI columns from pipeline data: theme chip, and (post-classification) a relevance marker (● Relevant 92% / ○ Not relevant) visually distinct from human tags. Sortable/filterable by both. The relevance pass becomes: sort by AI-relevant high-confidence, confirm or override, instead of Bates order.

### Context rail (right, ~380px, collapsible)

Replaces the AI FAB. Three states:

| Context | Rail contents |
|---|---|
| Nothing selected | Mini-brief, "Ask the production…" chat input, recent AI activity (e.g. classification progress) |
| One document focused | Summary, AI relevance decision + reasoning + key excerpts, Find similar, "Ask about this document" |
| Multiple selected | Count, "Ask about these N" (existing attachment-chip mechanism), bulk tag actions |

Chat opens in the rail, full-height, reusing `AIAgent.tsx` streaming/attachment UI (re-homed, not rewritten). Bulk-action floating bar keeps tag/export only; "Send to AI Agent" moves to the rail.

### Review workspace (header ✦ Review)

One page, two lanes that feed each other:

- **AI lane:** classification run status + cost, results table (ex-AIReviewPage): sorted by confidence, decision/reasoning/excerpts, accept/override per doc, bulk-accept above a confidence threshold.
- **Human lane:** queues/batches (ex-QueueManager/BatchReview). A queue can be created from an AI slice ("all AI-relevant ≥80% not yet human-confirmed").
- **Accepting an AI decision writes a real tag** — same namespace humans use, audit-logged as "accepted AI suggestion." Downstream export/filtering is agnostic about who decided. Until accepted, AI suggestions are visually separate (✦ mark) and live only in the AI columns/lane.

## 4. Visual system, errors, telemetry, testing

### Visual system

- **Inline styles → tokens/classes.** Replace hardcoded `rgba(44,62,107,…)` with existing custom properties; extract repeated inline blocks (header, bulk bar, footers) into `components.css` classes. New tokens: `--color-accent-brass`, AI-suggestion colors, rail dimensions.
- **One modal system** (`.modal-*`). The bespoke `.ai-agent-overlay` CSS dies with the FAB; the rail gets its own small class set.
- **One AI mark.** A single `✦` treatment (one class, size variants) replaces the six inconsistently-sized "AI" pills. Rule: ✦ = AI-generated and unconfirmed; disappears on human acceptance.
- **Theme palette.** CorpusAnalysis's 20 off-system hex colors are replaced by 8 muted archival hues derived from the parchment/ink system, cycling; used for theme chips, donut segments, and list markers.
- **Typography discipline.** Cormorant Garamond for page/brief/modal headlines only; Libre Franklin elsewhere; sizes from the token scale only.

### Error handling

Covered in §2 for the pipeline. Additionally: chat/stream errors keep current toast behavior; every AI surface has an explicit empty state ("Brief not generated yet — Generate now"), never blank space.

### Telemetry

New audit-log action types (no analytics vendor): `brief_generated`, `summary_batch_completed`, `classification_run`, `ai_suggestion_accepted`, `ai_suggestion_overridden`, `ai_chat_started`, `similar_docs_requested`. Feature adoption becomes answerable from the existing AuditLog UI.

### Testing

- Backend: pytest for pipeline orchestration, brief generation (mocked model calls), cost estimator, accept-suggestion→tag write path.
- Frontend: no new test framework (none exists today; lint is red on main). Every **touched** file must lint clean. Manual verification per phase against a seeded local production.

## Phasing (each phase ships usable)

1. **Frame** — command-bar header, ⚙ menu, case-desk picker, token/inline-style cleanup on touched surfaces.
2. **Ambient pipeline + Brief** — ingest step, auto-run stages, Brief on Home, retrofit button.
3. **Context rail** — rail + chat re-homing, FAB removal, list AI columns.
4. **Review workspace** — page merge, accept/override flow, cost-gated classification in the wizard.
5. **Sweep** — remaining restyles (viewer, dashboard, modals), onboarding guide rewrite, telemetry.

Each phase gets its own implementation plan; this spec is the umbrella document.

### Phase 5 cleanup notes (recorded during Phase 1 final review)

- Delete fully-dead CSS: `.search-toolbar`/`.search-row` (layout.css ~265-280 + mobile override ~980), `.saved-list`/`.saved-item` (~611-643), `.production-grid`/`.production-card*` (~718-765, ~1025). Keep `.app-header`/`.btn-header` until DocumentViewer, AIReviewPage, CorpusAnalysis, BatchReview, QCReview are restyled.
- Delete orphaned `nlSearch` in `frontend/src/api/client.ts` (backend `/api/ai/nl-search` endpoint has no UI caller).
- Collapse `--cb-height` and `--header-height` into one token when `.app-header` dies.
- `--color-brass-soft` currently unused — use or remove; spec §4 name reconciled: the shipped token is `--color-brass`.
- Menus: add Escape-to-close + arrow-key navigation (role="menu" semantics); logo span needs tabIndex/onKeyDown; saved/filter rows keyboard operability.
- Omnibox polish: pill snaps back to auto-detected mode after an overridden submit while results show the forced mode; add try/catch + user feedback on saved-search create/delete.

## Out of scope

- Dark mode.
- URL router library.
- New frontend test framework / fixing the pre-existing 41 lint errors beyond touched files.
- Privilege-review-specific features (the firm's jobs are orientation, relevance, hot docs).
- Analytics vendors; telemetry is audit-log only.
- NL→structured-search endpoint (`/api/ai/nl-search`) promotion — the omnibox uses the existing semantic-search path; revisit after adoption data exists.
