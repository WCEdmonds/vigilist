# Onboarding Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a paged feature-guide modal once per browser session after sign-in, with a permanent "Don't show again" opt-out and a header button to reopen it.

**Architecture:** Three new files split by responsibility — slide copy as data, a presentational modal that knows nothing about storage, and a hook that owns the storage keys and the should-show decision. Mounted once in `AppRouter` so it covers all three post-auth states (WelcomePage, ProductionPicker, Home).

**Tech Stack:** React 19, TypeScript 5.9, Vite 8, plain CSS with custom-property design tokens. No router, no state library, no test framework.

**Spec:** `docs/superpowers/specs/2026-07-16-onboarding-guide-design.md`

## Global Constraints

- **No automated tests in this project.** The repo has no frontend test framework — `frontend/package.json` has only `dev`, `build`, `lint`. Adding one is deferred to a follow-up project by explicit decision. **This plan therefore deviates from the usual TDD cycle: tasks are gated on `npm run build` (which runs `tsc -b`), a scoped lint check, and browser verification instead of on failing tests.** Do not add vitest/testing-library as part of this work.
- **The lint baseline is already failing — this is expected and not yours to fix.** `npm run lint` reports 41 errors and 8 warnings across 20 files on `origin/main`, predating this work (mostly `no-explicit-any` and `exhaustive-deps`). A whole-repo "lint is clean" gate is therefore unsatisfiable. The gate for every task is instead:
  - **New files must have zero lint errors and zero warnings.** Check with `npx eslint src/onboarding/slides.tsx src/components/OnboardingGuide.tsx src/hooks/useOnboarding.ts` (name only the files that exist yet).
  - **`src/App.tsx` must not exceed its recorded baseline of 7 errors / 1 warning.** Check with `npx eslint src/App.tsx`.
  - **`npm run build` must pass.**
  Do not fix pre-existing lint errors in files you touch — that is unrelated debt and would bury this diff. Do not silence errors with `eslint-disable` in new code; fix the code instead.
- Storage keys are namespaced per Firebase UID, exactly: `vigilist.onboarding.dismissed.<uid>` (localStorage) and `vigilist.onboarding.seen.<uid>` (sessionStorage).
- Every storage read and write is wrapped in `try/catch`. A storage failure must never prevent the app from booting. Read failure → treat as not dismissed/not seen (show the guide).
- Reuse existing CSS classes — `modal-overlay`, `modal-panel`, `modal-header`, `modal-body`, `modal-close-btn`, `btn`, `btn-primary`, `btn-secondary`, `btn-ghost`, `btn-sm`, `btn-header`, `visually-hidden`. Only add new classes where none exists (the footer and slide body).
- Use existing design tokens from `frontend/src/styles/variables.css`. Do not introduce new hex colors.
- All commands run from the `frontend/` directory unless stated otherwise.

## File Structure

| File | Responsibility |
|---|---|
| `frontend/src/onboarding/slides.tsx` (create) | Slide copy as data + the `Slide` type. Nothing else. |
| `frontend/src/components/OnboardingGuide.tsx` (create) | Presentational modal: paging, dots, checkbox, a11y. No storage. |
| `frontend/src/hooks/useOnboarding.ts` (create) | Storage keys + should-show decision. The only file touching those keys. |
| `frontend/src/styles/components.css` (modify) | Add `.modal-footer` and `.onboarding-*` classes. |
| `frontend/src/App.tsx` (modify) | Mount the guide in `AppRouter`; pass `onOpenGuide` to `Home`; header Guide button. |

---

### Task 1: Slide content

**Files:**
- Create: `frontend/src/onboarding/slides.tsx`

**Interfaces:**
- Consumes: nothing.
- Produces: `interface Slide { id: string; title: string; icon: string; body: ReactNode; ownerOnly?: boolean }` and `const SLIDES: Slide[]`. Task 2 imports `Slide`; Task 4 imports `SLIDES`.

- [ ] **Step 1: Create the slides file**

```tsx
import type { ReactNode } from 'react';

export interface Slide {
  id: string;
  /** Short heading shown above the body. */
  title: string;
  /** Emoji glyph — matches the existing WelcomePage feature icons. */
  icon: string;
  body: ReactNode;
  /** Only shown to users who own a production (or have none yet). */
  ownerOnly?: boolean;
}

export const SLIDES: Slide[] = [
  {
    id: 'welcome',
    title: 'Welcome to Vigilist',
    icon: '\u{1F4DA}',
    body: (
      <>
        <p>
          Vigilist is a document review platform for e-discovery productions. A{' '}
          <strong>production</strong> is one set of documents — everything you search,
          tag, and review lives inside one.
        </p>
        <p>
          This guide is a quick tour of what you can do. It takes about a minute, and
          you can reopen it any time from the <strong>Guide</strong> button in the header.
        </p>
      </>
    ),
  },
  {
    id: 'search',
    title: 'Search that understands you',
    icon: '\u{1F50D}',
    body: (
      <>
        <p>
          Type keywords for a <strong>full-text</strong> search. Ask a question in plain
          English — or type anything long — and Vigilist switches to{' '}
          <strong>semantic</strong> search, which finds documents by meaning rather than
          exact wording.
        </p>
        <p>
          We pick the mode for you, but you are never stuck with it: every result set has
          a <strong>Try semantic</strong> / <strong>Try full-text</strong> toggle to run
          the same query the other way.
        </p>
        <p>
          Narrow results by file type — email, PDF, video, audio, Office — and export any
          result set to CSV.
        </p>
      </>
    ),
  },
  {
    id: 'tagging',
    title: 'Tag and code in bulk',
    icon: '\u{1F3F7}',
    body: (
      <>
        <p>
          Tags carry a category — responsive, privilege, or your own custom ones. Create a
          tag on the fly whenever you need one.
        </p>
        <p>
          Tick the checkboxes on any rows and a bar appears at the bottom of the screen.
          From there you can tag, download the native files as a ZIP, or send the
          selection straight to the AI Agent.
        </p>
        <p>Filter the document list by tag, by file type, and sort by Bates number, recency, or size.</p>
      </>
    ),
  },
  {
    id: 'viewer',
    title: 'Read, annotate, and connect',
    icon: '\u{1F4C4}',
    body: (
      <>
        <p>
          Open any document to page through it, draw <strong>annotations</strong> on the
          page, leave <strong>notes</strong> for your team, and inspect the extracted
          metadata.
        </p>
        <p>
          <strong>Find similar</strong> pulls up documents that resemble the one you are
          reading — useful for chasing a thread once you have found one good hit. Titles
          are editable inline; the Bates numbers are not.
        </p>
      </>
    ),
  },
  {
    id: 'ai',
    title: 'AI that reads with you',
    icon: '\u{1F916}',
    body: (
      <>
        <p>
          The <strong>AI</strong> button in the bottom-right corner opens a chat panel.
          Attach documents to it — from the bulk bar, via{' '}
          <strong>Send to AI Agent</strong> — and ask questions about them.
        </p>
        <p>
          <strong>Smart Review</strong> has AI score documents for responsiveness before
          you read them, so the likely-relevant material rises to the top.
        </p>
        <p>
          <strong>Topic Groups</strong> and <strong>Corpus Analysis</strong> cluster the
          production by subject, which is a fast way to get the shape of a set you have
          never seen.
        </p>
      </>
    ),
  },
  {
    id: 'owner',
    title: 'Running a production',
    icon: '\u{2699}',
    ownerOnly: true,
    body: (
      <>
        <p>
          <strong>+ Ingest</strong> loads a new production. <strong>Share</strong> invites
          colleagues — invite someone who has not signed up yet and their access resolves
          automatically on first login.
        </p>
        <p>
          <strong>Review Queues</strong> split the work into batches and hand them to
          reviewers. The <strong>Dashboard</strong> tracks progress across the team, and
          the <strong>Audit Log</strong> records who did what.
        </p>
      </>
    ),
  },
];
```

- [ ] **Step 2: Verify it compiles**

Run: `npm run build`
Expected: build succeeds, no TypeScript errors. (The file is not imported yet; `tsc -b` still typechecks it.)

- [ ] **Step 3: Verify the new file lints clean**

Run: `npx eslint src/onboarding/slides.tsx`
Expected: no output (zero errors, zero warnings). Do not run the repo-wide
`npm run lint` as a gate — it fails on 41 pre-existing errors that are not yours.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/onboarding/slides.tsx
git commit -m "feat(onboarding): slide content for the feature guide"
```

---

### Task 2: The modal component and its styles

**Files:**
- Create: `frontend/src/components/OnboardingGuide.tsx`
- Modify: `frontend/src/styles/components.css` (append after the `.modal-close-btn` block, which currently ends at line 616)

**Interfaces:**
- Consumes: `Slide` from `../onboarding/slides` (Task 1).
- Produces: default export `OnboardingGuide`, props `{ slides: Slide[]; onClose: () => void; onDismissForever: () => void }`. Task 4 renders it.

**Behavior notes for the implementer:**
- The component owns only the current slide index and the checkbox state. It does not read or write storage — that is Task 3's job, reached through the two callbacks.
- Closing by any route (X, Esc, overlay click, Done) calls `onDismissForever` when the checkbox is ticked and `onClose` otherwise. One `finish()` function, used everywhere, so the checkbox cannot be bypassed by closing a different way.

- [ ] **Step 1: Create the component**

```tsx
import { useCallback, useEffect, useRef, useState } from 'react';
import type { Slide } from '../onboarding/slides';

interface Props {
  slides: Slide[];
  onClose: () => void;
  onDismissForever: () => void;
}

export default function OnboardingGuide({ slides, onClose, onDismissForever }: Props) {
  const [index, setIndex] = useState(0);
  const [dontShow, setDontShow] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  const slide = slides[index];
  const isLast = index === slides.length - 1;

  // Every exit path funnels through here so the checkbox is always honored.
  const finish = useCallback(() => {
    if (dontShow) onDismissForever();
    else onClose();
  }, [dontShow, onClose, onDismissForever]);

  // Esc closes, same as the X.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') finish();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [finish]);

  // Move focus into the dialog on open, and hand it back on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    return () => previous?.focus?.();
  }, []);

  if (!slide) return null;

  return (
    <div className="modal-overlay" onClick={finish}>
      <div
        ref={panelRef}
        className="modal-panel onboarding-panel"
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        tabIndex={-1}
      >
        <div className="modal-header">
          <h2
            id="onboarding-title"
            style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}
          >
            {slide.title}
          </h2>
          <button className="modal-close-btn" aria-label="Close guide" onClick={finish}>
            &times;
          </button>
        </div>

        <div className="modal-body">
          <div className="onboarding-icon" aria-hidden="true">{slide.icon}</div>
          <div className="onboarding-body">{slide.body}</div>
        </div>

        <div className="modal-footer">
          <label className="onboarding-dont-show">
            <input
              type="checkbox"
              checked={dontShow}
              onChange={e => setDontShow(e.target.checked)}
            />
            Don&apos;t show again
          </label>

          {/* Plain buttons, not role="tab" — ARIA tabs require a matching
              tabpanel and aria-controls, which these dots don't have. */}
          <div className="onboarding-dots">
            {slides.map((s, i) => (
              <button
                key={s.id}
                type="button"
                aria-current={i === index ? 'true' : undefined}
                aria-label={`Slide ${i + 1} of ${slides.length}: ${s.title}`}
                className={`onboarding-dot ${i === index ? 'active' : ''}`}
                onClick={() => setIndex(i)}
              />
            ))}
          </div>

          <div className="onboarding-nav">
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setIndex(i => i - 1)}
              disabled={index === 0}
            >
              Back
            </button>
            {isLast ? (
              <button className="btn btn-primary btn-sm" onClick={finish}>
                Done
              </button>
            ) : (
              <button className="btn btn-primary btn-sm" onClick={() => setIndex(i => i + 1)}>
                Next
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Append the styles**

Append to the end of `frontend/src/styles/components.css`:

```css
/* ── Onboarding guide ── */

.modal-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-4);
  border-top: 1px solid rgba(44, 62, 107, 0.1);
}

.onboarding-panel {
  width: 560px;
  max-width: 92vw;
}

.onboarding-icon {
  font-size: 40px;
  line-height: 1;
  text-align: center;
  margin-bottom: var(--space-4);
}

.onboarding-body {
  color: var(--color-neutral-600);
  font-size: var(--text-sm);
  line-height: 1.6;
  min-height: 190px;
}

.onboarding-body p {
  margin: 0 0 var(--space-3);
}

.onboarding-body p:last-child {
  margin-bottom: 0;
}

.onboarding-body strong {
  color: var(--color-ink);
  font-weight: 600;
}

.onboarding-dont-show {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-neutral-500);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}

.onboarding-dots {
  display: flex;
  gap: 6px;
}

.onboarding-dot {
  width: 7px;
  height: 7px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: var(--color-brand-200);
  cursor: pointer;
  transition: background 120ms ease, transform 120ms ease;
}

.onboarding-dot:hover {
  background: var(--color-brand-400);
}

.onboarding-dot.active {
  background: var(--color-ink);
  transform: scale(1.25);
}

.onboarding-dot:focus-visible {
  outline: 2px solid var(--color-ink);
  outline-offset: 2px;
}

.onboarding-nav {
  display: flex;
  gap: var(--space-2);
  white-space: nowrap;
}

/* The footer is crowded on a phone — stack it. */
@media (max-width: 560px) {
  .modal-footer {
    flex-wrap: wrap;
    justify-content: center;
  }
  .onboarding-body {
    min-height: 0;
  }
}
```

- [ ] **Step 3: Verify it compiles and lints clean**

Run: `npm run build`
Expected: succeeds.

Run: `npx eslint src/components/OnboardingGuide.tsx`
Expected: no output (zero errors, zero warnings). The component is not yet
mounted, so nothing changes visually.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/OnboardingGuide.tsx frontend/src/styles/components.css
git commit -m "feat(onboarding): guide modal component and styles"
```

---

### Task 3: The storage hook

**Files:**
- Create: `frontend/src/hooks/useOnboarding.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `useOnboarding(uid: string | undefined): { open: boolean; close: () => void; dismissForever: () => void; reopen: () => void }`. Task 4 calls it.

**Behavior notes for the implementer:**
- `localStorage` and `sessionStorage` are accessed through a getter inside the `try`, not passed in as a value. Reading the `window.localStorage` property *itself* throws when cookies are blocked, so `safeGet(localStorage, ...)` would throw at the call site, before the `try` could catch it. This is the whole reason the app cannot crash on boot.
- The auto-open decision is computed **once per mount via a `useState` lazy initializer**, not `useMemo`. It reads `seen`, and the effect then writes `seen` — if it recomputed after that write it would flip to `false` and close the modal mid-read. `useMemo` cannot be used: React documents it as a discardable performance cache with no semantic guarantee, so an eviction causes exactly that bug. A lazy initializer is guaranteed to run exactly once per mount.
- **A ref-based once-per-uid cache is not an option here.** This repo enables the React Compiler lint ruleset: `react-hooks/refs` (no ref access during render) and `react-hooks/set-state-in-render` are both errors. Both obvious workarounds are illegal. The lazy initializer is the only lint-legal shape that is also a real guarantee.
- Because the decision is captured at mount, **the hook must be mounted under a component that remounts when the user changes.** `AppRouter` satisfies this — it sits behind `AppContent`'s `!user` gate and Task 4 keys it by uid. Do not move the hook above that gate.
- `closedFor` / `forcedFor` hold a **uid**, not a boolean, so a stale flag cannot apply to a different user even within one mount.
- Storage presence is checked with `!== null`, not truthiness, so an empty-string value counts as stored.

> **Plan corrections (applied during execution):** this task originally prescribed
> `useMemo` for the decision and plain booleans for `closed`/`forced`. A review
> caught both. The first proposed fix (a `useRef` cache) was then rejected by
> `react-hooks/refs` — 7 errors. The code below is the third and shipped version.
> See the ledger's "Human decisions" section.

- [ ] **Step 1: Create the hook**

```ts
import { useCallback, useEffect, useState } from 'react';

const dismissedKey = (uid: string) => `vigilist.onboarding.dismissed.${uid}`;
const seenKey = (uid: string) => `vigilist.onboarding.seen.${uid}`;

/**
 * Storage access that cannot throw. Safari private mode and blocked cookies
 * make even reading the `localStorage` property throw, so the getter is
 * invoked inside the try — never call these with a bare `localStorage` value.
 *
 * A read failure reports "nothing stored", so the guide shows. Preferring to
 * show a guide over crashing the app at boot is deliberate.
 */
function safeGet(getStore: () => Storage, key: string): string | null {
  try {
    return getStore().getItem(key);
  } catch {
    return null;
  }
}

function safeSet(getStore: () => Storage, key: string, value: string): void {
  try {
    getStore().setItem(key, value);
  } catch {
    // Storage unavailable — the preference just won't persist.
  }
}

export interface OnboardingState {
  open: boolean;
  /** Close for this session only. */
  close: () => void;
  /** Tick "Don't show again" and close. Permanent. */
  dismissForever: () => void;
  /** Force open from the header button, ignoring both storage keys. */
  reopen: () => void;
}

function computeShouldAutoOpen(uid: string | undefined): boolean {
  if (!uid) return false;
  if (safeGet(() => localStorage, dismissedKey(uid)) !== null) return false;
  if (safeGet(() => sessionStorage, seenKey(uid)) !== null) return false;
  return true;
}

/**
 * PRECONDITION: must be mounted under a component that remounts when the
 * signed-in user changes. `AppRouter` satisfies this — it is gated on `user`
 * and keyed by uid. Do not move it above the `!user` gate in `AppContent`.
 */
export function useOnboarding(uid: string | undefined): OnboardingState {
  // Keyed by uid rather than plain booleans, so a stale flag cannot apply to a
  // different user.
  const [closedFor, setClosedFor] = useState<string | undefined>(undefined);
  const [forcedFor, setForcedFor] = useState<string | undefined>(undefined);

  // Decided exactly once per mount. A lazy initializer is a React semantic
  // guarantee; useMemo would NOT be.
  const [shouldAutoOpen] = useState(() => computeShouldAutoOpen(uid));

  // Mark seen as soon as we decide to show it, so a refresh within this
  // session doesn't bring it back.
  useEffect(() => {
    if (uid && shouldAutoOpen) safeSet(() => sessionStorage, seenKey(uid), '1');
  }, [uid, shouldAutoOpen]);

  const close = useCallback(() => {
    setForcedFor(undefined);
    setClosedFor(uid);
  }, [uid]);

  const dismissForever = useCallback(() => {
    if (uid) safeSet(() => localStorage, dismissedKey(uid), '1');
    setForcedFor(undefined);
    setClosedFor(uid);
  }, [uid]);

  // Reopening does NOT clear the dismissal — asking to see it once is not
  // asking to have it thrown at you every session again.
  const reopen = useCallback(() => {
    setClosedFor(undefined);
    setForcedFor(uid);
  }, [uid]);

  const closed = uid !== undefined && closedFor === uid;
  const forced = uid !== undefined && forcedFor === uid;
  const open = forced || (shouldAutoOpen && !closed);

  return { open, close, dismissForever, reopen };
}
```

- [ ] **Step 2: Verify it compiles and lints clean**

Run: `npm run build`
Expected: succeeds.

Run: `npx eslint src/hooks/useOnboarding.ts`
Expected: no output (zero errors, zero warnings).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/useOnboarding.ts
git commit -m "feat(onboarding): session/dismissal storage hook"
```

---

### Task 4: Wire it into the app

**Files:**
- Modify: `frontend/src/App.tsx` — `Home` props and header (around lines 34–38 and 318–328), and `AppRouter` (lines 876–957)

**Interfaces:**
- Consumes: `SLIDES` from `./onboarding/slides` (Task 1), `OnboardingGuide` from `./components/OnboardingGuide` (Task 2), `useOnboarding` from `./hooks/useOnboarding` (Task 3), `useAuth` from `./hooks/useAuth` (existing).
- Produces: nothing downstream.

**Why `AppRouter` and not `Home`:** `AppRouter` is the only component rendering across all three post-auth states. Mounting in `Home` would miss anyone parked on the production picker or the welcome page.

- [ ] **Step 1: Add the imports**

In `frontend/src/App.tsx`, alongside the existing component imports:

```tsx
import OnboardingGuide from './components/OnboardingGuide';
import { SLIDES } from './onboarding/slides';
import { useOnboarding } from './hooks/useOnboarding';
```

- [ ] **Step 2: Add the `onOpenGuide` prop to `Home`**

Change the `HomeProps` interface (currently lines 34–38):

```tsx
interface HomeProps {
  production: ProductionInfo;
  onSwitchProduction: () => void;
  onIngestComplete: () => void;
  onOpenGuide: () => void;
}

function Home({ production, onSwitchProduction, onIngestComplete, onOpenGuide }: HomeProps) {
```

- [ ] **Step 3: Add the Guide button to the header**

In the `desktop-only` button group in `Home`'s header, add a Guide button immediately after the existing Dashboard button:

```tsx
<button className="btn-header" style={{ background: 'rgba(255,255,255,0.7)' }} onClick={() => setShowDashboard(true)}>Dashboard</button>
<button className="btn-header" style={{ background: 'rgba(255,255,255,0.7)' }} onClick={onOpenGuide}>Guide</button>
```

- [ ] **Step 4: Restructure `AppRouter` to mount the guide once**

`AppRouter` currently returns three separate fragments, each with its own `<ToastContainer />`. Collect the branches into a `content` variable so the guide mounts in exactly one place instead of being pasted three times.

Replace the body of `AppRouter` from the `if (prodLoading)` block through the end of the function with:

```tsx
  const { user } = useAuth();
  const { open: guideOpen, close: closeGuide, dismissForever, reopen: openGuide } = useOnboarding(user?.uid);

  // Someone with zero productions is about to ingest and become an owner —
  // they are exactly who needs the owner slide.
  const showOwnerSlides = productions.length === 0 || productions.some(p => p.is_owner);
  const slides = useMemo(
    () => SLIDES.filter(s => showOwnerSlides || !s.ownerOnly),
    [showOwnerSlides],
  );

  // Don't show the guide over a loading spinner.
  if (prodLoading) {
    return (
      <div className="loading-fullscreen">
        <span className="spinner" />
        <div>Loading productions…</div>
      </div>
    );
  }

  let content: ReactNode;
  if (productions.length === 0) {
    content = <WelcomePage onIngest={() => setShowIngestWizard(true)} />;
  } else if (!activeProduction) {
    content = (
      <ProductionPicker
        productions={productions}
        onSelect={setActiveProduction}
        onIngest={() => setShowIngestWizard(true)}
        onDeleted={loadProductions}
      />
    );
  } else {
    content = (
      <Home
        production={activeProduction}
        onSwitchProduction={() => setActiveProduction(null)}
        onIngestComplete={handleIngestComplete}
        onOpenGuide={openGuide}
      />
    );
  }

  return (
    <>
      {content}
      {showIngestWizard && (
        <IngestWizard onClose={() => setShowIngestWizard(false)} onComplete={handleIngestComplete} />
      )}
      {guideOpen && (
        <OnboardingGuide
          slides={slides}
          onClose={closeGuide}
          onDismissForever={dismissForever}
        />
      )}
      <ToastContainer />
    </>
  );
}
```

Note: the ingest wizard was previously rendered only in the first two branches. Hoisting it is safe — `Home` renders its own wizard off its own `showIngestWizard` state, and `AppRouter`'s copy stays closed unless `WelcomePage` or `ProductionPicker` opens it.

- [ ] **Step 5: Add the `ReactNode` type import**

The `content` variable needs the type. Update the React import at the top of `App.tsx`:

```tsx
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
```

- [ ] **Step 6: Verify it compiles and lints within baseline**

Run: `npm run build`
Expected: succeeds.

Run: `npx eslint src/App.tsx`
Expected: **at most** 7 errors and 1 warning — App.tsx's recorded pre-existing
baseline. If the count went up, your change introduced it; fix that. Do not fix
the 7 pre-existing errors, and do not add `eslint-disable` comments.

Run: `npx eslint src/onboarding/slides.tsx src/components/OnboardingGuide.tsx src/hooks/useOnboarding.ts`
Expected: no output.

- [ ] **Step 7: Verify in the browser**

Run: `npm run dev`

Work through each of these against the running app. Open DevTools → Application → Storage to inspect and clear keys.

1. Clear both `vigilist.onboarding.*` keys, reload → **guide appears**.
2. Page through with Next/Back; click the dots → **slide changes**.
3. As an owner (a production where `is_owner` is true), confirm the **"Running a production"** slide is present. As a reviewer with no owned productions, confirm it is **absent**.
4. Close with X (checkbox unticked) → reload → **does not reappear** (`...seen.<uid>` is set in sessionStorage).
5. Clear `...seen.<uid>`, reload → guide returns → tick **Don't show again** → Done → reload → **does not reappear** (`...dismissed.<uid>` is set in localStorage).
6. Click the header **Guide** button → **opens** despite both keys being set. Close it → confirm `...dismissed.<uid>` is **still set**.
7. Press **Esc** → closes, and focus returns to the Guide button.
8. Open a private window with cookies blocked (`chrome://settings/cookies` → "Block all cookies") → **app still boots**, guide shows, no console errors.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat(onboarding): mount guide in AppRouter with header reopen button"
```

---

## Follow-ups (not this project)

- **Frontend test framework.** Deferred by decision. `useOnboarding` is the natural first target — it is pure logic with no rendering, and the storage-throws path is exactly the sort of thing nobody re-tests by hand.
- **Consolidate `WelcomePage`.** It pitches Search / Tag & Code / AI Tools, which now overlaps slides 2, 3, and 5. Left alone deliberately to keep this project narrow.
- **Server-side dismissal.** If per-browser dismissal proves annoying, add a column to `users` and return it from `/api/auth/sync`, which already runs on every login. Costs an Alembic migration run manually against Neon prod.
