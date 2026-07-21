# Onboarding Guide — Design

**Date:** 2026-07-16
**Status:** Approved, pending implementation plan

## Problem

Vigilist has a wide feature surface — full-text and semantic search, tagging and
bulk coding, the document viewer with annotations and notes, the AI Agent, Smart
Review, Topic Groups / Corpus Analysis, Review Queues and batches, the Dashboard,
Ingest, Share, and the Audit Log. Nothing introduces any of it. A user who is
invited to a production lands in `Home` and is expected to infer the whole app
from the header.

A `WelcomePage` component already exists and pitches three features (Search,
Tag & Code, AI Tools), but `App.tsx` renders it only when `productions.length === 0`.
Any user with at least one production — which is every invited reviewer — never
sees it. The introduction that exists is unreachable by the people who need it.

## Goal

Show a short, paged feature guide when a user arrives in the app, once per
browser session, with a permanent "Don't show again" opt-out and a way to
reopen it on demand.

## Non-goals

- Automated tests. There is no frontend test framework today (`package.json`
  has only `dev`, `build`, `lint`; no vitest, no testing-library). Adding one is
  deliberately deferred to a follow-up project covering the whole frontend, not
  bolted on here. This project is verified by driving the app in a browser.
- Server-side persistence of the dismissal (see Decisions).
- Replacing or reworking `WelcomePage`. It stays as-is for the zero-productions
  case; the guide layers on top of it.

## Decisions

### Format: modal carousel

A centered modal with paged slides, dot indicators, Back/Next, a close button,
and a "Don't show again" checkbox in the footer.

Rejected: an interactive spotlight tour anchored to real DOM nodes. It teaches
location rather than concept, which is genuinely better, but it must attach to
elements that differ between owners and reviewers (`Share`, `Audit Log`, and
`+ Ingest` are conditionally rendered) and between desktop and mobile (several
header controls are `desktop-only`). The modal is decoupled from the DOM and
survives layout changes.

Rejected: a dismissible banner linking to a full guide page. Least intrusive,
and therefore least effective — most users would never click through.

### Persistence: localStorage, namespaced per user

The dismissal lives in `localStorage`, not on the `users` table.

Trade-off accepted knowingly: this is per-browser. Dismissing in Chrome does not
dismiss in Edge, and clearing site data brings the guide back. The alternative —
a column on `users` returned via `/api/auth/sync`, which already runs on every
login — would follow the user across devices, but costs an Alembic migration
that must be run manually against Neon in production. Not worth it for a
cosmetic preference.

Keys, both namespaced by Firebase UID so that two people sharing a browser do
not inherit each other's state:

| Key | Store | Meaning |
|---|---|---|
| `vigilist.onboarding.dismissed.<uid>` | `localStorage` | Permanent opt-out. Set only by the checkbox. |
| `vigilist.onboarding.seen.<uid>` | `sessionStorage` | Seen this session. Set on mount. |

### Trigger: once per browser session

The guide opens when: a user is signed in, `dismissed` is unset, and `seen` is
unset. `seen` is written on mount, so a refresh or a re-opened tab within the
same session does not re-trigger it. Ending the browser session clears `seen`,
so the next sign-in shows it again — which is the "each time a user logs in"
behavior that was asked for, minus the part where refreshing to check ingest
progress makes the modal reappear.

Rejected: firing on the auth signed-out → signed-in transition. Firebase
persists sessions, so most users would almost never see it.

### Reopening

"Don't show again" is permanent and there is no server record, so without an
escape hatch a user who dismisses on day one can never find the guide again.
A **Guide** button in the app header calls `reopen()`, which opens the modal
regardless of either storage key. Reopening does not clear the dismissal — the
user opted out, and opening it manually is not a request to opt back in.

## Storage error handling

`localStorage` and `sessionStorage` throw in Safari private mode and when
cookies are blocked. An uncaught throw at app startup would take down the
entire app, so every read and write is wrapped in `try/catch`.

- **Read throws** → treat as not dismissed and not seen → show the guide.
- **Write throws** → swallow; the dismissal does not persist.

Failing toward showing is deliberate. A guide that reappears is a nuisance; an
app that will not boot is an outage.

## Architecture

Three files, split so that storage logic, presentation, and copy are
independently changeable:

### `frontend/src/hooks/useOnboarding.ts`

Owns the storage keys and the should-show decision. Nothing else in the app
touches those keys.

```ts
function useOnboarding(uid: string | undefined): {
  open: boolean;
  close: () => void;            // dismiss for this session only
  dismissForever: () => void;   // tick "Don't show again" + close
  reopen: () => void;           // force open, ignores both keys
}
```

### `frontend/src/components/OnboardingGuide.tsx`

Presentational only. Knows nothing about storage.

```ts
interface Props {
  slides: Slide[];
  onClose: () => void;
  onDismissForever: () => void;
}
```

Renders the modal, tracks the current slide index internally, and renders the
"Don't show again" checkbox. Ticking the checkbox and closing calls
`onDismissForever`; closing without it calls `onClose`.

### `frontend/src/onboarding/slides.tsx`

Slide content as data, so copy edits never touch behavior.

```ts
interface Slide {
  id: string;
  title: string;
  body: ReactNode;
  icon: ReactNode;
  ownerOnly?: boolean;
}
```

## Mount point

`AppRouter` (`frontend/src/App.tsx`), not `Home`.

`AppRouter` is the only component that renders across all three post-auth
states — `WelcomePage` (no productions), `ProductionPicker` (several), and
`Home` (one active) — so mounting there means the guide appears regardless of
where the user lands. Mounting in `Home` would miss anyone stopped at the
production picker.

Owner slides are gated on:

```ts
const showOwnerSlides = productions.length === 0 || productions.some(p => p.is_owner);
```

`AppRouter` already holds `productions`, so this needs no new fetching. The
zero-productions case resolving to `true` is intentional: that user is about to
ingest and become an owner, so they are precisely who needs the Ingest slide.

## Content — 6 slides

1. **Welcome to Vigilist** — what the app is; productions contain documents.
2. **Search** — full-text vs. semantic; the auto-detect heuristic (question
   words, `?`, or >40 chars routes to semantic); the "Try semantic/full-text"
   toggle so they know they can override it; metadata filters.
3. **Tag & code** — tags by category (responsive, privilege, custom); select
   rows to reveal the floating bulk bar; bulk tag, download, export CSV; filter
   and sort.
4. **The viewer** — page navigation, annotations, notes, metadata panel,
   find-similar, editable titles.
5. **AI** — the floating AI Agent launcher; "Send to AI Agent" from a selection;
   Smart Review for AI-scored responsiveness; Topic Groups and Corpus Analysis.
6. **Running a production** *(ownerOnly)* — Ingest, Share, Review Queues and
   batches, Dashboard, Audit Log.

## Accessibility

- `role="dialog"`, `aria-modal="true"`, `aria-labelledby` pointing at the slide title.
- Esc closes (session-only dismiss, same as the X).
- Focus moves into the modal on open, returns to the trigger on close.
- Dot indicators are buttons with accessible labels, not bare divs.

## Verification

No automated tests this project. Verified by driving the real app:

1. Sign in with both keys cleared → guide appears.
2. Page through all slides; confirm owner slide is present for an owner and
   absent for a reviewer with no owned productions.
3. Close without the checkbox → reload → does not reappear (session `seen` set).
4. Close with "Don't show again" → new browser session → does not reappear.
5. Header **Guide** button → opens regardless of both keys.
6. Esc closes; focus returns to the trigger.
7. Block cookies / private window → app still boots, guide shows, no crash.
