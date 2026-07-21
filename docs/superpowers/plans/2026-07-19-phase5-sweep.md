# Phase 5 "Sweep" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the redesign: telemetry + classify retry on the backend, the rail drawer and interaction polish, restyles of every remaining old-UI surface (DocumentViewer, BatchReview, QCReview, modals, Welcome), dead-CSS/token cleanup, and a truthful onboarding guide.

**Architecture:** Backend first (audit actions incl. an actor-resolution helper for ambient runs, bounded rate-limit retry in `classify_document`, endpoint tests). Then frontend in dependency order: small consistency fixes → rail drawer (restoring the sub-1024px Ask path) → chat memoization + keyboard a11y → DocumentViewer restyle (kills the last `.app-header` consumer) → BatchReview/QCReview onto a shared `.fullscreen-bar` → modal/Welcome mini-pass → dead-CSS + token collapse → onboarding rewrite → sweep.

**Tech Stack:** unchanged (FastAPI/SQLAlchemy/pytest; React 19 + token CSS).

**Spec:** `docs/superpowers/specs/2026-07-16-ui-redesign-ambient-ai-design.md` §4 (visual system, telemetry names), §1 page-inventory, spec appendix "Phase 5 cleanup notes", ledger carry-list in `.superpowers/sdd/progress.md`.

## Global Constraints

- No new dependencies; backend tests `cd backend && python -m pytest tests/ -q` (baseline 79 passed + the 1 known failure `test_ai_review.py::test_build_classification_prompt` — leave it); frontend `npx eslint <touched>` → 0 errors, NO eslint-disable, no setState-in-effect; `npm run build` green per task.
- No hardcoded colors in touched TSX (fix on contact); ✦ is the AI mark — the remaining `.ai-indicator` "AI" pills on touched surfaces become ✦ (`.brief-ai-mark` class or `.ai-marker-star`).
- Audit actions (spec §4, verbatim): `ai_chat_started`, `similar_docs_requested`, `brief_generated`, `summary_batch_completed`. `log_action` never commits; SSE endpoints must log+commit BEFORE returning the StreamingResponse (session dies once streaming starts).
- Typography: `--font-serif` for page/modal titles only; sizes from tokens.
- Behavior preserved everywhere: this phase restyles and polishes — zero functional regressions in viewer/batch/QC/modals; onboarding auto-open/dismiss/reopen mechanics untouched (copy only).
- Commit per task with the given message.

## File Structure (by task)

T1 backend: `services/audit.py`, `services/pipeline.py`, `services/ai_review.py`, `routers/ai.py`, + tests. T2: `routers/review.py` (bulk-accept batching), `App.tsx` (marker color), `HumanReviewLane.tsx` (literals). T3: `ContextRail.tsx`, `App.tsx`, `layout.css`. T4: `useChat.ts`, `AppHeader.tsx`, `Omnibox.tsx`, `ProductionSettings.tsx`. T5: `DocumentViewer.tsx`, `MetadataPanel.tsx`, `layout.css`. T6: `BatchReview.tsx`, `QCReview.tsx`, `layout.css`. T7: `Dashboard.tsx`, `AuditLog.tsx`, `ManageAccess.tsx`, `WelcomePage.tsx`. T8: `layout.css`, `variables.css`, `components.css`. T9: `onboarding/slides.tsx`. T10: none.

---

### Task 1: Backend telemetry, classify retry, endpoint tests

**Files:** Modify `backend/app/services/audit.py`, `backend/app/services/pipeline.py`, `backend/app/services/ai_review.py`, `backend/app/routers/ai.py`. Create `backend/tests/test_audit_actor.py`, `backend/tests/test_classify_retry.py`, `backend/tests/test_review_endpoints.py`.

**Interfaces:**
- `resolve_audit_actor(db, production) -> User | None` in audit.py: returns the production's owner User row (via `db.get(User, production.owner_id)`) or None when `owner_id` is null — ambient pipeline actions are logged AS the owner, or skipped when no owner exists. Document that choice in the docstring.
- Pipeline: in `_run_brief` after the brief is persisted (pipeline.py:~115): load the production's owner via `resolve_audit_actor`; if found, `log_action(db, actor, "brief_generated", "production", str(production_id), production_id=production_id, details={"model": brief.get("model")})` + commit in the same session block. In `_run_summaries`, once at stage completion (after the final batch commit): `summary_batch_completed` with `details={"summarized": <count written this run>}` (track a counter), same actor pattern.
- `routers/ai.py`: `chat()` — after doc resolution, before returning the StreamingResponse: `ai_chat_started` (`resource_type="production"` when docs exist → `documents[0].production_id`, else `resource_id=None, production_id=None`), `details={"doc_count": len(doc_ids)}`, then `await db.commit()`. `find_similar()` — `similar_docs_requested` on the doc + explicit commit.
- `ai_review.py` `classify_document`: bounded retry INSIDE the function — wrap `client.messages.create` in `for attempt in range(3):` catching `anthropic.RateLimitError`, `anthropic.APIStatusError`, `anthropic.APIConnectionError` (import lazily alongside the client) with `await asyncio.sleep(2 * (attempt + 1))` between attempts; after exhaustion fall through to the existing zero-token sentinel. Any other exception: no retry, sentinel immediately. The `tokens == 0` worker contract is unchanged.
- Endpoint tests (ledger carry): FakeSession-style unit tests for `GET /estimate` (avg-None → zeros) and `POST /auto-classify` (409 duplicate name; 400 empty case_context) — monkeypatch `get_user_role_for_production` and fake `db` per the house pattern in `backend/tests/test_results_ownership.py` (read it first).

- [ ] **Step 1 (TDD):** Write the three test files first; run → ImportError/failures (capture). Tests: `test_audit_actor.py` — actor resolves owner; returns None when owner_id None (FakeSession w/ `db.get` AsyncMock). `test_classify_retry.py` — patch `app.services.ai_review._get_client`... (the module lazy-imports `anthropic` inline — patch `anthropic.AsyncAnthropic` via the module's import mechanism or patch the client factory if one exists; READ classify_document first and structure the test so `messages.create` raises `anthropic.RateLimitError` twice then succeeds → assert 3 calls and a real result; and raises 3× → assert sentinel `(parse of "{}", 0)`; construct RateLimitError instances per the SDK's signature — if constructing SDK error types is impractical in tests, refactor classify_document to catch a module-level `_RETRYABLE_ERRORS` tuple you can monkeypatch to `(ValueError,)` in tests — document whichever you do). `test_review_endpoints.py` per Interfaces.
- [ ] **Step 2:** Implement all four file changes.
- [ ] **Step 3:** `python -m pytest tests/ -q` → prior passes + new tests, same 1 known failure; `python -c "from app.main import app"` → 0.
- [ ] **Step 4:** Commit: `feat(api): remaining audit telemetry, classify retry, review endpoint tests`

---

### Task 2: Small consistency bundle (bulk-accept batching, marker color, lane literals)

**Files:** Modify `backend/app/routers/review.py` (bulk_accept), `frontend/src/App.tsx` (aiMarker), `frontend/src/components/HumanReviewLane.tsx`.

**Interfaces / behavior:**
- `bulk_accept`: before the loop, (a) prefetch existing `(document_id, tag_id)` pairs for the candidate docs in ONE query and pass a `skip_pairs: set` down; (b) cache resolved tags per category in a dict so `resolve_tag_for_category` runs once per distinct category. Add optional params to `apply_decision_tag(db, user, result, decision, project, *, tag_cache: dict | None = None, existing_pairs: set | None = None)` — defaulting to None preserves the single-decide path unchanged. Suite must stay green (test_review_tags.py exercises the resolver purely).
- `App.tsx` `aiMarker`: `key_document` text color becomes `var(--color-primary-400)` (blue-family, matching DEFAULT_CATEGORIES' declared blue and the AI lane) instead of success-green; `relevant` stays success.
- `HumanReviewLane.tsx`: replace color literals — `'white'` → `var(--color-neutral-0)`, `background: ... : 'white'` likewise; the delete-confirm button's `color: 'white'` → `var(--color-neutral-0)`. No other changes.

- [ ] **Step 1:** Implement; `python -m pytest tests/ -q` baseline; frontend eslint (App.tssx→App.tsx, HumanReviewLane.tsx) 0 errors; build green.
- [ ] **Step 2:** Commit: `fix: bulk-accept batching, key_document color parity, lane color tokens`

---

### Task 3: Rail drawer below 1025px

**Files:** Modify `frontend/src/components/ContextRail.tsx`, `frontend/src/App.tsx`, `frontend/src/styles/layout.css`.

**Interfaces / behavior:**
- Replace the `display:none` hiding (layout.css:1665-1679) with a drawer: below 1025px the rail renders as `position: fixed; top: var(--cb-height); right: 0; bottom: 0; width: min(var(--rail-width), 90vw); z-index: 200; box-shadow: var(--shadow-xl); transform: translateX(100%); transition: transform var(--transition-slow);` and `.context-rail.is-drawer-open { transform: none; }`. The collapsed tab becomes a fixed edge tab at `right: 0; top: 40%;` (z-index 150) with the ✦ — always visible below 1025px, toggling the drawer. DELETE the `.omnibox-ask { display:none }` rule — the Ask path works again (focusChat opens the drawer: `onToggleCollapsed`/`focusChat` semantics unchanged — expanding = opening the drawer at small widths since both are `!collapsed`).
- Implementation: pure CSS where possible — the same `collapsed` state drives both modes; add class `is-drawer-open` mirroring `!collapsed` on the rail root (a single className change in ContextRail). Desktop behavior unchanged (media-scoped rules). The floating-bar desktop shift rule stays scoped ≥1025px.
- A close affordance inside the drawer header (the existing collapse button suffices — verify it's visible in drawer mode).

- [ ] **Step 1:** CSS + className changes; eslint/build; manually reason the matrix in the report: desktop expand/collapse unchanged; <1025px tab shows, opens drawer over content, Ask AI expands drawer + focuses composer.
- [ ] **Step 2:** Commit: `feat(frontend): context rail drawer below desktop width`

---

### Task 4: Chat memoization + keyboard a11y bundle

**Files:** Modify `frontend/src/hooks/useChat.ts`, `frontend/src/components/AppHeader.tsx`, `frontend/src/components/Omnibox.tsx`, `frontend/src/components/ProductionSettings.tsx`.

**Behavior:**
- `useChat`: wrap the returned object in `useMemo` keyed on every field/callback it contains (callbacks are already useCallback-stable; state values change appropriately) so `chat` identity only changes when its contents do. `ChatState` shape unchanged.
- AppHeader + Omnibox menus: Escape closes any open menu (single `keydown` listener active only while a menu is open); menu buttons get `onKeyDown` Enter/Space handled natively (they're `<button>`s — verify, no change needed); the logo span gains `tabIndex={0}` + `onKeyDown` Enter → `onLogoClick`.
- Omnibox saved/filter rows (`<div onClick>`): convert the clickable saved-search row to a `<button type="button" class="dropdown-item omnibox-saved-item">` (full-width reset classes already exist for `.omnibox-menu button.dropdown-item`) keeping the inner delete button (nested interactive is invalid — restructure: row button + sibling delete button inside a flex wrapper div, matching the visual layout).
- ProductionSettings: wrap fields in a `<form onSubmit={...}>` so Enter saves; keep buttons type="button"/"submit" correct; Esc behavior unchanged.
- Stale-comment cleanup rides along: in useChat.ts/ContextRail.tsx, rewrite comments citing `AIAgent.tsx:<line>` as prose ("the retired overlay's ...").

- [ ] **Step 1:** Implement; eslint on the four files → 0 errors; build; report a focus-walk trace (Tab through header: logo → switcher → omnibox → pills → actions → gear; Esc closes menus).
- [ ] **Step 2:** Commit: `fix(frontend): chat state memoization and menu keyboard accessibility`

---

### Task 5: DocumentViewer restyle + AI-tools fold

**Files:** Modify `frontend/src/components/DocumentViewer.tsx`, `frontend/src/components/MetadataPanel.tsx`, `frontend/src/styles/layout.css`.

**Behavior:**
- Desktop header (DocumentViewer.tsx:359-366): replace the `.app-header` row with a `.viewer-bar` — ink bar visually matching `.command-bar` (new CSS class copying the command-bar look: ink background, parchment text, 52px via `--cb-height`): left `← Back` (`.cb-action`), center-left serif "Vigilist" logo span (non-interactive here), right `Download File` (`.cb-action`). This removes the LAST `.app-header` consumer (Task 8 deletes the CSS).
- AI tools fold: delete the footer block (L476-488); `MetadataPanel` gains an "✦ AI tools" section at its bottom — props `onSummarize: () => void`, `onFindSimilar?: () => void`, `summarizing: boolean`, `findingSimilar: boolean` threaded from DocumentViewer (state/handlers already exist there). Buttons: "✦ Summarize" (hidden when a summary already exists — check `doc.summary`, pass `hasSummary: boolean`), "✦ Find similar" (rendered only when `onFindSimilar` provided). Replace the `.ai-indicator` pills with the ✦ convention.
- The Summary tab behavior unchanged. Mobile branch untouched.

- [ ] **Step 1:** Implement; eslint (DocumentViewer, MetadataPanel) 0 errors; build; grep `app-header` in frontend/src → ZERO consumers left (CSS deletion happens in Task 8).
- [ ] **Step 2:** Commit: `feat(frontend): viewer ink bar and AI tools folded into metadata panel`

---

### Task 6: BatchReview + QCReview restyle

**Files:** Modify `frontend/src/components/BatchReview.tsx`, `frontend/src/components/QCReview.tsx`, `frontend/src/styles/layout.css`.

**Behavior:**
- New shared CSS `.fullscreen-bar` (layout.css): ink bar like `.command-bar` (background `var(--color-ink)`, parchment text, height `var(--cb-height)`, flex, gap, padding) + `.fullscreen-bar .fs-title` (serif, `--text-lg`) + progress text slot (`--font-mono --text-xs`).
- BatchReview: its inline-styled dark header (L139-142 region) becomes `.fullscreen-bar` with `← Back to Batches` as `.cb-action` (fixing the currently-unstyled `.btn-header`), serif queue title, reviewed-count progress. Reduce the heaviest repeated inline styles in the header/footer rows to classes where they repeat 3+ times; do NOT restructure the review flow.
- QCReview: same treatment (`Close` → `.cb-action`; the `rgba(255,255,255,0.2)` progress track → `rgba` replaced with a class using `var(--color-parchment-light)` at reduced opacity via `color-mix(in srgb, var(--color-parchment-light) 20%, transparent)` — if `color-mix` feels risky, an `opacity: 0.2` overlay div achieves it; choose one and note it).
- Zero behavior changes — same handlers, same flow, same keyboard shortcuts if any.

- [ ] **Step 1:** Implement; eslint 0 errors; build; grep `btn-header` → remaining consumers only where styled (DocumentViewer is gone after T5; ReviewWorkspace keeps its scoped one).
- [ ] **Step 2:** Commit: `feat(frontend): batch and QC review on the ink fullscreen bar`

---

### Task 7: Modal + Welcome consistency mini-pass

**Files:** Modify `frontend/src/components/Dashboard.tsx`, `frontend/src/components/AuditLog.tsx`, `frontend/src/components/ManageAccess.tsx`, `frontend/src/components/WelcomePage.tsx`.

**Behavior (light touch — consistency only, NO layout rework):**
- All three modals: title uses the shared `.modal-title` class (serif) if not already; any `.ai-indicator` pills → ✦; any hex/rgba color literals in TSX → tokens (explorer found 0 hex — verify and fix stragglers found on contact); Dashboard's 56 inline styles: extract ONLY repeated (3+ occurrences) patterns into classes, leave one-offs.
- WelcomePage: copy check — it mentions "Ingest a Production" button (still true); ensure its header uses `.welcome-header` (unchanged) and its CTA styling matches `.btn btn-primary` (verify).

- [ ] **Step 1:** Implement; eslint on the four files 0 errors; build.
- [ ] **Step 2:** Commit: `style(frontend): modal and welcome consistency pass`

---

### Task 8: Dead CSS + token collapse

**Files:** Modify `frontend/src/styles/layout.css`, `frontend/src/styles/variables.css`, `frontend/src/styles/components.css`.

**Behavior (each deletion grep-gated — skip with a note if a consumer appears):**
- Delete: `.search-toolbar`/`.search-row` block (layout.css:265-282 + any mobile override), `.saved-list`/`.saved-item`/`.query`/`.delete-btn` (611-641), `.production-grid`/`.production-card*` (718-760 + 1025 mobile), `.app-header` block (7-61) + its mobile block (892-915) — Task 5 removed the last consumer.
- Token collapse: `.review-workspace-header` switches `--header-height` → `--cb-height`; then delete `--header-height` from variables.css (grep first). Keep `--sidebar-width` (viewer uses it) and `--color-brass-soft` (IngestWizard uses it).
- `grep -rn "app-header\|search-toolbar\|saved-list\|saved-item\|production-grid\|production-card\|header-height" frontend/src` → zero hits after.

- [ ] **Step 1:** Implement; build green (CSS-only, but tsc runs anyway); visual spot-risk is low — note any class whose deletion you skipped and why.
- [ ] **Step 2:** Commit: `chore(frontend): retire dead legacy CSS and collapse header tokens`

---

### Task 9: Onboarding guide rewrite

**Files:** Modify `frontend/src/onboarding/slides.tsx` (copy only — `Slide` interface and OnboardingGuide mechanics untouched).

New copy (binding; JSX with the existing `<p>`/`<strong>` conventions of the current file — read it and match formatting):

1. `welcome` (📚) "Welcome to Vigilist" — "Vigilist is a document review platform for e-discovery productions. A **production** is one set of documents — everything you search, tag, and review lives inside one." + "This guide takes about a minute. Reopen it anytime from the **⚙ menu → Guide**."
2. `search` (🔍) "Search, or just ask" — "The search box in the top bar understands both **full-text** queries (\"phrases\", AND/OR/NOT, wildcard*) and plain questions. Type a question and the pill flips to **✦ Ask** — press **✦ Ask AI** to send it to the AI chat instead of searching. Narrow by file type, save searches, and export results to CSV from the results header."
3. `brief` (✦) "Your production, already read" — NEW SLIDE replacing the old `tagging` position order (keep tagging too — see 4): "When a production is ingested, AI clusters it into **themes**, summarizes every document, and writes a **Production Brief** at the top of Home — who's involved, what it spans, what stands out. Click a theme chip to filter the list. Owners can regenerate from the brief card."
4. `tagging` (🏷️) "Tag and code in bulk" — "Select documents with the checkboxes and a bar appears at the bottom: tag them, download a ZIP, or clear the selection. Titles are inline-editable. AI suggestions you accept become ordinary tags — same colors, same filters."
5. `viewer` (📄) "Read, annotate, and connect" — "Open any document to page through it, drop pin annotations, write notes, and inspect metadata. **✦ AI tools** in the sidebar summarize the document or find similar ones across the production."
6. `rail` (💬) "The Intelligence rail" — NEW: "The right-hand rail follows your work: with nothing selected, ask the production anything; select one document for its summary and quick actions; select several to ask about them together. Collapse it with the ✦ tab — your conversation stays until you switch productions."
7. `review` (✅) "Review, two lanes" — REWRITE of old `ai` slide: "**✦ Review** in the top bar opens the workspace. The AI lane classifies documents against your case description — sort by confidence, agree or override (accepting writes a real tag), bulk-accept above a threshold, or cut a review queue from any slice. The human lane holds queues and batches for your team."
8. `owner` (⚙️, ownerOnly) "Running a production" — "Everything administrative lives in the **⚙ menu**: ingest a new production, share access, production settings (your case description), the audit log, and this guide. **Dashboard** in the top bar tracks progress. When you ingest, describe the case — the AI uses it for the brief and classification, and you'll get a cost estimate before anything runs."

Slide count goes 6 → 8; the `SLIDES` array order above is binding; ids/icons as shown; `ownerOnly: true` only on `owner`.

- [ ] **Step 1:** Rewrite; eslint 0 errors; build; confirm OnboardingGuide dots render 8 (it derives from array length — verify no hardcoded 6).
- [ ] **Step 2:** Commit: `docs(frontend): onboarding guide rewritten for the redesigned UI`

---

### Task 10: Phase verification sweep

- [ ] **Step 1:** `python -m pytest tests/ -q` (all new tests green, 1 known failure); `python -m alembic heads` single head; `npm run build`; eslint over all Phase 5 touched files → 0 errors; greps from Tasks 5/8 re-run clean.
- [ ] **Step 2:** Live pass (Chrome required — THIS is the combined P3+P4+P5 gate; checklists in `.superpowers/sdd/progress.md` + P4 final review): everything from those lists PLUS: viewer ink bar + AI tools in metadata panel; batch/QC bars; rail drawer at <1025px incl. Ask path; onboarding 8 slides truthfulness walk; audit log shows the four new action types after exercising chat/find-similar/pipeline.
- [ ] **Step 3:** Commit fixes if any: `fix: phase 5 verification fixes`

---

## Self-Review Notes

- Spec §4 telemetry: all four missing actions land in T1 (ambient actor = production owner via `resolve_audit_actor`; ownerless ambient runs skip — documented deviation consistent with P2's "ambient runs skip audit" precedent, now narrowed to ownerless only). §1 inventory: viewer restyle+fold T5, modals kept-and-restyled T7, guide rewrite T9. Spec appendix items: dead CSS T8, token collapse T8, `--color-brass-soft` resolved as LIVE (kept — appendix updated understanding), menus Escape/keyboard T4, omnibox pill polish deliberately dropped (cosmetic, results header already shows truth — documented cut), saved-search error handling landed in T4 via row-button conversion? NO — error handling on create/delete writes: ADD to T4: wrap `handleSave`/delete in try/catch + toasts. (Added to T4 scope.)
- Ledger carries: drawer T3, memoized ChatState T4, per-doc busy keys — CUT (accepted; single-doc visibility makes it cosmetic), stale comments T4, lane literals T2, bulk-accept batching T2, key_document color T2, classify retry + endpoint tests T1, focus-steal — CUT (accepted behavior), ProductionSettings form-Enter T4, `.case-card-confirm` rgba — left (white scrim, accepted), formatAdded double-call — CUT (pure/cheap).
- Type consistency: `apply_decision_tag` keyword-only extension is backward-compatible; MetadataPanel prop names match T5's thread list; `.fullscreen-bar`/`.cb-action` reuse verified against existing classes.
