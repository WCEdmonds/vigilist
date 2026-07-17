# Phase 1 "Frame" Implementation Plan — Command-Bar Header, Case-Desk Picker, Token Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Vigilist's 7-button header with an ink-blue command bar (production switcher + search-or-ask omnibox + gear menu), rebuild the production picker as a "case desk" landing page, and move touched surfaces off inline styles onto CSS tokens.

**Architecture:** A new `AppHeader` component (command bar) is shared by Home and ProductionPicker; the existing `SearchBar` is replaced by an `Omnibox` that makes the semantic/full-text auto-detection visible and overridable. Mode detection is extracted to a shared util so Home's `handleSearch` and the Omnibox hint agree. The backend `GET /api/productions` gains a `document_count` per production for the case-desk cards. Navigation stays state-driven in `App.tsx` — no router.

**Tech Stack:** React 19 + TypeScript + Vite, plain CSS with custom-property tokens (no Tailwind, no component libraries), FastAPI + SQLAlchemy (async) + pytest on the backend.

**Spec:** `docs/superpowers/specs/2026-07-16-ui-redesign-ambient-ai-design.md` (§1 Information architecture; §4 visual system rules).

## Global Constraints

- No new npm dependencies; no router library; no frontend test framework.
- Every touched frontend file must pass `npx eslint <file>` with zero errors (repo-wide lint is red on main — do not try to fix untouched files).
- `npm run build` (tsc + vite) must pass after every frontend task.
- No hardcoded `rgba(44, 62, 107, …)` or hex colors in **new/modified** TSX — use CSS classes and `var(--…)` tokens. Inline `style={{}}` allowed only for truly dynamic values.
- Typography: `var(--font-serif)` for display headings only; body/UI text stays `var(--font-sans)`.
- The `✦` character is the AI mark (spec §4) — never the old `AI` pill in new code.
- Backend: run tests with `cd backend && python -m pytest tests/ -v`.
- Frontend commands run from `frontend/`: `npm run build`, `npx eslint src/...`.
- Existing behavior that must not regress: URL state restore (`?prod=&q=&doc=&batch=&view=`), owner-only gating of Share/Audit, delete-production flow, "Send to AI Agent" bulk action, onboarding Guide reopen.
- Commit after every task with the message given in the task.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `backend/app/schemas.py` | modify | `ProductionWithAccess` gains `document_count` |
| `backend/app/routers/productions.py` | modify | count documents per production in `list_productions` |
| `backend/tests/test_productions_list.py` | create | unit test for the enriched listing |
| `frontend/src/types/index.ts` | modify | `ProductionInfo.document_count` |
| `frontend/src/utils/searchMode.ts` | create | `detectSearchMode()` shared by Home + Omnibox |
| `frontend/src/styles/variables.css` | modify | brass accent tokens |
| `frontend/src/styles/layout.css` | modify | `.command-bar` styles; `.case-desk` styles (old `.app-header` / `.production-*` rules stay for other screens until Phase 5) |
| `frontend/src/components/Omnibox.tsx` | create | header search box: visible mode hint/toggle, saved searches, metadata filters |
| `frontend/src/components/AppHeader.tsx` | create | command bar: logo, production switcher, omnibox slot, ✦ Review, Dashboard, gear menu, avatar |
| `frontend/src/components/SearchBar.tsx` | **delete** | superseded by Omnibox (only importer is App.tsx) |
| `frontend/src/App.tsx` | modify | Home renders AppHeader; AppRouter passes productions down; old header + search row removed |
| `frontend/src/components/ProductionPicker.tsx` | modify | case-desk landing page |

**Phase-boundary notes (do NOT build these now):** the gear menu gets "Production settings" in Phase 2; the TopicGroups strip and the "Review Queues" gear item are removed in later phases; case-desk cards get theme summaries/pipeline status in Phase 2. Leave code comments only where a later phase must plug in.

---

### Task 1: Backend — `document_count` on production listing

**Files:**
- Modify: `backend/app/schemas.py:218-226`
- Modify: `backend/app/routers/productions.py:24-49`
- Test: `backend/tests/test_productions_list.py` (create)

**Interfaces:**
- Produces: `GET /api/productions` items now include `document_count: int` (0 when a production has no documents). Frontend Task 2 mirrors this in `ProductionInfo`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_productions_list.py`:

```python
"""Unit test for list_productions document_count enrichment.

Uses a fake session (no database) in the same spirit as test_org_access.py.
get_accessible_production_ids is monkeypatched so the fake session only has
to answer the two queries list_productions itself issues: the Production
select and the per-production document count.
"""

import asyncio
from datetime import datetime, timezone

import app.routers.productions as productions_router


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"


class FakeProduction:
    def __init__(self, pid, name, owner_id):
        self.id = pid
        self.name = name
        self.description = None
        self.owner_id = owner_id
        self.created_at = datetime(2026, 7, 1, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _query):
        return FakeResult(self._results.pop(0))


def test_list_productions_includes_document_count(monkeypatch):
    async def fake_ids(db, user):
        return [1, 2]

    monkeypatch.setattr(
        productions_router, "get_accessible_production_ids", fake_ids
    )

    prods = [FakeProduction(1, "Acme v. Barrett", "u1"), FakeProduction(2, "Smith", "u2")]
    # Second query result: (production_id, count) tuples — prod 2 has no docs.
    db = FakeSession([prods, [(1, 4218)]])

    out = asyncio.run(
        productions_router.list_productions(db=db, user=FakeUser("u1"))
    )

    assert [p.document_count for p in out] == [4218, 0]
    assert out[0].is_owner is True
    assert out[1].is_owner is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_productions_list.py -v`
Expected: FAIL — `ValidationError` / `TypeError` (`document_count` unknown) or `AttributeError: document_count`.

- [ ] **Step 3: Add the schema field**

In `backend/app/schemas.py`, add one line to `ProductionWithAccess`:

```python
class ProductionWithAccess(BaseModel):
    id: int
    name: str
    description: str | None
    owner_id: str | None
    is_owner: bool = False
    created_at: datetime
    document_count: int = 0

    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Compute counts in the router**

In `backend/app/routers/productions.py`: add `func` to the sqlalchemy import and `Document` to the models import:

```python
from sqlalchemy import func, select
```
```python
from app.models import Document, PendingInvite, Production, ProductionAccess, User
```

Replace the body of `list_productions` after `prods = result.scalars().all()`:

```python
    counts_result = await db.execute(
        select(Document.production_id, func.count(Document.id))
        .where(Document.production_id.in_(prod_ids))
        .group_by(Document.production_id)
    )
    counts = dict(counts_result.all())
    return [
        ProductionWithAccess(
            id=p.id,
            name=p.name,
            description=p.description,
            owner_id=p.owner_id,
            is_owner=(p.owner_id == user.id),
            created_at=p.created_at,
            document_count=counts.get(p.id, 0),
        )
        for p in prods
    ]
```

(If `Document.production_id` is named differently in `app/models.py`, check the model and use the actual FK column; the test's tuple shape stays the same.)

- [ ] **Step 5: Run the new test and the full backend suite**

Run: `cd backend && python -m pytest tests/test_productions_list.py -v` → PASS
Run: `cd backend && python -m pytest tests/ -v` → all pass (pre-existing failures, if any, must be listed in the commit message and left alone).

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas.py backend/app/routers/productions.py backend/tests/test_productions_list.py
git commit -m "feat(api): include document_count in production listing"
```

---

### Task 2: Frontend groundwork — type, tokens, searchMode util

**Files:**
- Modify: `frontend/src/types/index.ts:94-101`
- Modify: `frontend/src/styles/variables.css`
- Create: `frontend/src/utils/searchMode.ts`
- Modify: `frontend/src/App.tsx:144-158` (Home's `handleSearch` uses the util)

**Interfaces:**
- Produces: `detectSearchMode(query: string): SearchMode` and `type SearchMode = 'fulltext' | 'semantic'` — consumed by App.tsx (this task) and Omnibox (Task 3).
- Produces: CSS tokens `--color-brass`, `--color-brass-soft`, `--cb-height` — consumed by Tasks 3–6 styles.
- Produces: `ProductionInfo.document_count: number` — consumed by Task 6.

- [ ] **Step 1: Add `document_count` to ProductionInfo**

In `frontend/src/types/index.ts`:

```typescript
export interface ProductionInfo {
  id: number;
  name: string;
  description: string | null;
  owner_id: string | null;
  is_owner: boolean;
  created_at: string;
  document_count: number;
}
```

- [ ] **Step 2: Add tokens**

In `frontend/src/styles/variables.css`, after the `--color-purple-*` block (line 77), insert:

```css
  /* ── Accent (brass — avatar chip, warm highlights) ── */
  --color-brass: #c49a4a;
  --color-brass-soft: rgba(196, 154, 74, 0.18);
```

and in the `/* ── Layout ── */` block add:

```css
  --cb-height: 52px;
```

- [ ] **Step 3: Create the searchMode util**

Create `frontend/src/utils/searchMode.ts` with the exact heuristic currently inlined at `App.tsx:152-157`:

```typescript
export type SearchMode = 'fulltext' | 'semantic';

/**
 * Heuristic used everywhere a query's mode is auto-detected: long queries,
 * question words, or a question mark read as "asking the production"
 * (semantic); everything else is full-text.
 */
export function detectSearchMode(query: string): SearchMode {
  return query.length > 40
    || /\b(what|where|who|when|why|how|which|find|show|any|all)\b/i.test(query)
    || query.includes('?')
    ? 'semantic'
    : 'fulltext';
}
```

- [ ] **Step 4: Use it in Home's handleSearch**

In `frontend/src/App.tsx` add the import:

```typescript
import { detectSearchMode, type SearchMode } from './utils/searchMode';
```

Change the `handleSearch` signature and mode line (currently `App.tsx:144-158`) to:

```typescript
  const handleSearch = async (query: string, metadata?: Record<string, string>, forceMode?: SearchMode) => {
    setLoading(true);
    setSearchQuery(query);
    setHasSearched(true);
    setSelectedIds(new Set());
    setLastMetadata(metadata);

    const mode = forceMode ?? detectSearchMode(query);
    setLastSearchMode(mode);
```

Also change the `lastSearchMode` state declaration (App.tsx:56) to use the shared type:

```typescript
  const [lastSearchMode, setLastSearchMode] = useState<SearchMode>('fulltext');
```

- [ ] **Step 5: Verify**

Run: `cd frontend && npx eslint src/utils/searchMode.ts src/types/index.ts src/App.tsx` → 0 errors
Run: `cd frontend && npm run build` → succeeds

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/styles/variables.css frontend/src/utils/searchMode.ts frontend/src/App.tsx
git commit -m "feat(frontend): shared search-mode util, brass tokens, document_count type"
```

---

### Task 3: Omnibox component

**Files:**
- Create: `frontend/src/components/Omnibox.tsx`
- Modify: `frontend/src/styles/layout.css` (append omnibox styles)

**Interfaces:**
- Consumes: `detectSearchMode`, `SearchMode` from `../utils/searchMode`; `createSavedSearch`, `deleteSavedSearch`, `getSavedSearches` from `../api/client`; `SavedSearch` from `../types`.
- Produces: `<Omnibox onSearch={(query, metadata?, forceMode?) => void} initialQuery?: string />` — mounted by AppHeader (Task 4). `onSearch` has the exact signature of Home's `handleSearch` after Task 2.

Replaces `SearchBar.tsx`. Feature parity: saved searches (list/save/delete), metadata key/value filters. Dropped deliberately: the `nlMode` "AI" toggle button and `nlSearch` call (spec: out of scope; the omnibox mode toggle supersedes it). New: the auto-detected mode is *shown* while typing and can be flipped before submitting.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/Omnibox.tsx`:

```tsx
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createSavedSearch, deleteSavedSearch, getSavedSearches } from '../api/client';
import { detectSearchMode, type SearchMode } from '../utils/searchMode';
import type { SavedSearch } from '../types';

interface Props {
  onSearch: (query: string, metadata?: Record<string, string>, forceMode?: SearchMode) => void;
  initialQuery?: string;
}

/**
 * Header search-or-ask box. Auto-detects full-text vs semantic ("ask") mode
 * as the user types and shows it as a clickable pill so the choice is
 * visible and overridable — the override applies to the next submit only.
 */
export default function Omnibox({ onSearch, initialQuery = '' }: Props) {
  const [query, setQuery] = useState(initialQuery);
  const [modeOverride, setModeOverride] = useState<SearchMode | null>(null);
  const [savedSearches, setSavedSearches] = useState<SavedSearch[]>([]);
  const [menu, setMenu] = useState<'none' | 'saved' | 'filters'>('none');
  const [saveName, setSaveName] = useState<string | null>(null);
  const [metadataFilters, setMetadataFilters] = useState<Record<string, string>>({});
  const [filterKey, setFilterKey] = useState('');
  const [filterValue, setFilterValue] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => { setQuery(initialQuery); setModeOverride(null); }, [initialQuery]);

  const loadSaved = () => { getSavedSearches().then(setSavedSearches).catch(() => {}); };
  useEffect(loadSaved, []);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setMenu('none');
        setSaveName(null);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const mode: SearchMode = modeOverride ?? detectSearchMode(query);
  const filterCount = Object.keys(metadataFilters).length;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setMenu('none');
    onSearch(query.trim(), filterCount > 0 ? metadataFilters : undefined, mode);
    setModeOverride(null);
  };

  const handleSave = async () => {
    if (!saveName?.trim() || !query.trim()) return;
    await createSavedSearch(saveName.trim(), query.trim());
    setSaveName(null);
    loadSaved();
  };

  return (
    <div className="omnibox" ref={rootRef}>
      <form onSubmit={submit} className="omnibox-row">
        <input
          type="text"
          className="omnibox-input"
          value={query}
          onChange={e => { setQuery(e.target.value); setModeOverride(null); }}
          placeholder="Search, or ask a question…"
          aria-label="Search or ask a question"
        />
        {query.trim() && (
          <button
            type="button"
            className={`omnibox-mode ${mode === 'semantic' ? 'is-ask' : ''}`}
            onClick={() => setModeOverride(mode === 'semantic' ? 'fulltext' : 'semantic')}
            title="Toggle between full-text search and asking the production"
          >
            {mode === 'semantic' ? '✦ Ask' : 'Text'}
          </button>
        )}
        <button
          type="button"
          className={`omnibox-tool ${filterCount > 0 ? 'is-active' : ''}`}
          onClick={() => setMenu(menu === 'filters' ? 'none' : 'filters')}
          title="Metadata filters"
        >
          Filters{filterCount > 0 ? ` (${filterCount})` : ''}
        </button>
        <button
          type="button"
          className="omnibox-tool"
          onClick={() => setMenu(menu === 'saved' ? 'none' : 'saved')}
          title="Saved searches"
        >
          Saved
        </button>
      </form>

      {menu === 'saved' && (
        <div className="dropdown omnibox-menu">
          {query.trim() && (
            saveName === null ? (
              <button type="button" className="dropdown-item" onClick={() => setSaveName(query)}>
                ＋ Save current search
              </button>
            ) : (
              <form
                className="omnibox-save-row"
                onSubmit={e => { e.preventDefault(); handleSave(); }}
              >
                <input
                  className="input input-sm"
                  value={saveName}
                  onChange={e => setSaveName(e.target.value)}
                  autoFocus
                  aria-label="Saved search name"
                />
                <button type="submit" className="btn btn-primary btn-xs">Save</button>
              </form>
            )
          )}
          {savedSearches.length === 0 && !query.trim() && (
            <div className="dropdown-item omnibox-empty">No saved searches yet.</div>
          )}
          {savedSearches.map(ss => (
            <div
              key={ss.id}
              className="dropdown-item omnibox-saved-item"
              onClick={() => { setQuery(ss.query); setMenu('none'); onSearch(ss.query); }}
            >
              <div className="omnibox-saved-text">
                <div className="omnibox-saved-name">{ss.name}</div>
                <div className="omnibox-saved-query">{ss.query}</div>
              </div>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                aria-label={`Delete saved search ${ss.name}`}
                onClick={async e => { e.stopPropagation(); await deleteSavedSearch(ss.id); loadSaved(); }}
              >×</button>
            </div>
          ))}
        </div>
      )}

      {menu === 'filters' && (
        <div className="dropdown omnibox-menu">
          {Object.entries(metadataFilters).map(([k, v]) => (
            <div key={k} className="dropdown-item omnibox-saved-item">
              <span className="omnibox-saved-text">{k}: {v}</span>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                aria-label={`Remove filter ${k}`}
                onClick={() => {
                  const next = { ...metadataFilters };
                  delete next[k];
                  setMetadataFilters(next);
                }}
              >×</button>
            </div>
          ))}
          <form
            className="omnibox-save-row"
            onSubmit={e => {
              e.preventDefault();
              if (filterKey.trim() && filterValue.trim()) {
                setMetadataFilters({ ...metadataFilters, [filterKey.trim()]: filterValue.trim() });
                setFilterKey('');
                setFilterValue('');
              }
            }}
          >
            <input className="input input-sm" placeholder="Field" value={filterKey} onChange={e => setFilterKey(e.target.value)} aria-label="Filter field name" />
            <input className="input input-sm" placeholder="Value" value={filterValue} onChange={e => setFilterValue(e.target.value)} aria-label="Filter value" />
            <button type="submit" className="btn btn-secondary btn-xs">Add</button>
          </form>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Append omnibox styles**

At the end of `frontend/src/styles/layout.css` add:

```css
/* ── Omnibox (command-bar search) ── */

.omnibox {
  position: relative;
  flex: 1;
  max-width: 560px;
}

.omnibox-row {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  background: var(--color-neutral-0);
  border-radius: var(--radius-md);
  padding: 0 var(--space-2) 0 var(--space-3);
  height: 34px;
  box-shadow: var(--shadow-inner), 0 0 0 1px rgba(44, 62, 107, 0.25);
}
.omnibox-row:focus-within {
  box-shadow: var(--shadow-inner), var(--shadow-ring);
}

.omnibox-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  color: var(--color-neutral-700);
}
.omnibox-input::placeholder {
  color: var(--color-neutral-400);
}

.omnibox-mode {
  flex-shrink: 0;
  border: 1px solid var(--color-neutral-200);
  background: var(--color-neutral-50);
  color: var(--color-neutral-500);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  padding: 2px var(--space-2);
  border-radius: var(--radius-full);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}
.omnibox-mode.is-ask {
  border-color: var(--color-brand-200);
  background: var(--color-brand-50);
  color: var(--color-ink);
}

.omnibox-tool {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: var(--color-neutral-400);
  font-size: var(--text-xs);
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition-fast);
}
.omnibox-tool:hover,
.omnibox-tool.is-active {
  color: var(--color-ink);
}

.omnibox-menu {
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  max-height: 320px;
  overflow-y: auto;
}

.omnibox-save-row {
  display: flex;
  gap: var(--space-1);
  padding: var(--space-2);
  border-top: 1px solid var(--color-neutral-100);
}

.omnibox-saved-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
}

.omnibox-saved-text {
  flex: 1;
  overflow: hidden;
}

.omnibox-saved-name {
  font-weight: var(--font-semibold);
  font-size: var(--text-sm);
}

.omnibox-saved-query {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--color-neutral-400);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.omnibox-empty {
  color: var(--color-neutral-400);
  cursor: default;
}
```

- [ ] **Step 3: Verify**

Run: `cd frontend && npx eslint src/components/Omnibox.tsx` → 0 errors
Run: `cd frontend && npm run build` → succeeds (component is not mounted yet; build proves types).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/Omnibox.tsx frontend/src/styles/layout.css
git commit -m "feat(frontend): Omnibox search-or-ask component with visible mode toggle"
```

---

### Task 4: AppHeader command bar

**Files:**
- Create: `frontend/src/components/AppHeader.tsx`
- Modify: `frontend/src/styles/layout.css` (append command-bar styles)

**Interfaces:**
- Consumes: `Omnibox` (Task 3), `UserAvatar` (existing: `name`, `email`, `photoUrl`, `size` props), `useAuth` (provides `user`, `logout`), `ProductionInfo`, `SearchMode`.
- Produces: the component below — mounted by Home and ProductionPicker in Tasks 5–6.

```tsx
interface AppHeaderProps {
  production?: ProductionInfo;               // undefined on the case-desk page
  productions: ProductionInfo[];
  onSelectProduction?: (p: ProductionInfo) => void;
  onShowAllProductions?: () => void;
  onSearch?: (query: string, metadata?: Record<string, string>, forceMode?: SearchMode) => void;
  onLogoClick?: () => void;                  // Home passes clearSearch
  initialQuery?: string;
  onOpenReview?: () => void;
  onOpenDashboard?: () => void;
  onOpenShare?: () => void;                  // pass only when owner
  onOpenAudit?: () => void;                  // pass only when owner
  onOpenQueues?: () => void;                 // interim home for Review Queues (removed in Phase 4)
  onOpenIngest?: () => void;
  onOpenGuide?: () => void;
  onRandomDoc?: () => void;
}
```

- [ ] **Step 1: Write the component**

Create `frontend/src/components/AppHeader.tsx`:

```tsx
import { useEffect, useRef, useState } from 'react';
import Omnibox from './Omnibox';
import UserAvatar from './UserAvatar';
import { useAuth } from '../hooks/useAuth';
import type { ProductionInfo } from '../types';
import type { SearchMode } from '../utils/searchMode';

interface AppHeaderProps {
  production?: ProductionInfo;
  productions: ProductionInfo[];
  onSelectProduction?: (p: ProductionInfo) => void;
  onShowAllProductions?: () => void;
  onSearch?: (query: string, metadata?: Record<string, string>, forceMode?: SearchMode) => void;
  onLogoClick?: () => void;
  initialQuery?: string;
  onOpenReview?: () => void;
  onOpenDashboard?: () => void;
  onOpenShare?: () => void;
  onOpenAudit?: () => void;
  onOpenQueues?: () => void;
  onOpenIngest?: () => void;
  onOpenGuide?: () => void;
  onRandomDoc?: () => void;
}

/**
 * The command bar: brand, production switcher, search-or-ask omnibox,
 * the two daily-use actions (Review, Dashboard), and a gear menu holding
 * everything administrative. Shared by Home and the case-desk picker.
 */
export default function AppHeader({
  production,
  productions,
  onSelectProduction,
  onShowAllProductions,
  onSearch,
  onLogoClick,
  initialQuery,
  onOpenReview,
  onOpenDashboard,
  onOpenShare,
  onOpenAudit,
  onOpenQueues,
  onOpenIngest,
  onOpenGuide,
  onRandomDoc,
}: AppHeaderProps) {
  const { user, logout } = useAuth();
  const [openMenu, setOpenMenu] = useState<'none' | 'switcher' | 'gear'>('none');
  const switcherRef = useRef<HTMLDivElement>(null);
  const gearRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const t = e.target as Node;
      if (switcherRef.current?.contains(t) || gearRef.current?.contains(t)) return;
      setOpenMenu('none');
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const gearItems = [
    onOpenShare && { label: 'Share…', action: onOpenShare },
    onOpenIngest && { label: '＋ Ingest a production', action: onOpenIngest },
    onOpenQueues && { label: 'Review queues', action: onOpenQueues },
    onRandomDoc && { label: 'Random document', action: onRandomDoc },
    onOpenAudit && { label: 'Audit log', action: onOpenAudit },
    onOpenGuide && { label: 'Guide', action: onOpenGuide },
  ].filter(Boolean) as { label: string; action: () => void }[];

  return (
    <header className="command-bar">
      <span
        className="command-bar-logo"
        onClick={onLogoClick}
        role={onLogoClick ? 'button' : undefined}
      >
        Vigilist
      </span>

      {production && (
        <div className="cb-switcher" ref={switcherRef}>
          <button
            type="button"
            className="cb-switcher-btn"
            onClick={() => setOpenMenu(openMenu === 'switcher' ? 'none' : 'switcher')}
            aria-haspopup="menu"
            aria-expanded={openMenu === 'switcher'}
          >
            {production.name}
            {productions.length > 1 && <span className="cb-caret">▾</span>}
          </button>
          {openMenu === 'switcher' && (
            <div className="dropdown cb-menu" role="menu">
              {productions.map(p => (
                <button
                  key={p.id}
                  type="button"
                  role="menuitem"
                  className={`dropdown-item ${p.id === production.id ? 'is-current' : ''}`}
                  onClick={() => { setOpenMenu('none'); if (p.id !== production.id) onSelectProduction?.(p); }}
                >
                  {p.name}
                </button>
              ))}
              {onShowAllProductions && (
                <button
                  type="button"
                  role="menuitem"
                  className="dropdown-item cb-menu-footer"
                  onClick={() => { setOpenMenu('none'); onShowAllProductions(); }}
                >
                  All productions…
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {onSearch && <Omnibox onSearch={onSearch} initialQuery={initialQuery} />}

      <div className="cb-actions">
        {onOpenReview && (
          <button type="button" className="cb-action cb-action-primary" onClick={onOpenReview}>
            ✦ Review
          </button>
        )}
        {onOpenDashboard && (
          <button type="button" className="cb-action" onClick={onOpenDashboard}>
            Dashboard
          </button>
        )}
        <div className="cb-gear" ref={gearRef}>
          <button
            type="button"
            className="cb-action cb-icon"
            onClick={() => setOpenMenu(openMenu === 'gear' ? 'none' : 'gear')}
            aria-haspopup="menu"
            aria-expanded={openMenu === 'gear'}
            aria-label="Settings and tools"
          >
            ⚙
          </button>
          {openMenu === 'gear' && (
            <div className="dropdown cb-menu cb-menu-right" role="menu">
              <div className="cb-menu-user">{user?.displayName || user?.email}</div>
              {gearItems.map(item => (
                <button
                  key={item.label}
                  type="button"
                  role="menuitem"
                  className="dropdown-item"
                  onClick={() => { setOpenMenu('none'); item.action(); }}
                >
                  {item.label}
                </button>
              ))}
              <button
                type="button"
                role="menuitem"
                className="dropdown-item cb-menu-footer"
                onClick={logout}
              >
                Sign out
              </button>
            </div>
          )}
        </div>
        <span className="cb-avatar">
          <UserAvatar name={user?.displayName ?? null} email={user?.email ?? ''} photoUrl={user?.photoURL} size={28} />
        </span>
      </div>
    </header>
  );
}
```

- [ ] **Step 2: Append command-bar styles**

At the end of `frontend/src/styles/layout.css` add:

```css
/* ── Command Bar (Phase 1 header) ── */

.command-bar {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  height: var(--cb-height);
  padding: 0 var(--space-5);
  background: var(--color-ink);
  color: var(--color-parchment-light);
  flex-shrink: 0;
}

.command-bar-logo {
  font-family: var(--font-serif);
  font-size: var(--text-xl);
  font-weight: var(--font-bold);
  letter-spacing: 0.01em;
  color: var(--color-parchment-light);
  cursor: pointer;
  transition: opacity var(--transition-fast);
  flex-shrink: 0;
}
.command-bar-logo:hover {
  opacity: 0.8;
}

.cb-switcher {
  position: relative;
  flex-shrink: 0;
}

.cb-switcher-btn {
  border: none;
  background: rgba(245, 240, 232, 0.1);
  color: var(--color-parchment-light);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  padding: 5px var(--space-3);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast);
  max-width: 280px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cb-switcher-btn:hover {
  background: rgba(245, 240, 232, 0.18);
}

.cb-caret {
  margin-left: var(--space-1);
  opacity: 0.6;
  font-size: var(--text-xs);
}

.cb-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}

.cb-action {
  border: 1px solid rgba(245, 240, 232, 0.3);
  background: transparent;
  color: var(--color-parchment-light);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  padding: 5px var(--space-3);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.cb-action:hover {
  background: rgba(245, 240, 232, 0.12);
  border-color: rgba(245, 240, 232, 0.5);
}

.cb-action-primary {
  border-color: rgba(245, 240, 232, 0.55);
}

.cb-icon {
  border: none;
  font-size: var(--text-lg);
  padding: 3px var(--space-2);
}

.cb-gear {
  position: relative;
}

.cb-menu {
  top: calc(100% + 8px);
  left: 0;
  min-width: 220px;
}

.cb-menu-right {
  left: auto;
  right: 0;
}

.cb-menu .dropdown-item.is-current {
  font-weight: var(--font-semibold);
  color: var(--color-ink);
}

.cb-menu-user {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  color: var(--color-neutral-400);
  border-bottom: 1px solid var(--color-neutral-100);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cb-menu-footer {
  border-top: 1px solid var(--color-neutral-100);
}

.cb-avatar {
  display: inline-flex;
  border-radius: var(--radius-full);
  box-shadow: 0 0 0 2px var(--color-brass);
}

@media (max-width: 720px) {
  .command-bar {
    gap: var(--space-2);
    padding: 0 var(--space-3);
  }
  .cb-switcher-btn {
    max-width: 130px;
  }
  .cb-action:not(.cb-icon) {
    display: none;
  }
}
```

Note: `.dropdown` (components.css:359) is position-absolute with white background — `.cb-menu` and `.omnibox-menu` both build on it; menus render on light background even though the bar is ink.

- [ ] **Step 3: Verify**

Run: `cd frontend && npx eslint src/components/AppHeader.tsx` → 0 errors
Run: `cd frontend && npm run build` → succeeds

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AppHeader.tsx frontend/src/styles/layout.css
git commit -m "feat(frontend): AppHeader command bar with switcher and gear menu"
```

---

### Task 5: Wire the command bar into Home, delete the old header and SearchBar

**Files:**
- Modify: `frontend/src/App.tsx` (Home props/JSX, AppRouter)
- Delete: `frontend/src/components/SearchBar.tsx`

**Interfaces:**
- Consumes: `AppHeader` (Task 4), `SearchMode` (Task 2).
- Produces: `HomeProps` gains `productions: ProductionInfo[]` and `onSelectProduction: (p: ProductionInfo) => void` — AppRouter supplies both.

- [ ] **Step 1: Update imports in App.tsx**

Remove the `SearchBar` import (line 16). Add:

```typescript
import AppHeader from './components/AppHeader';
```

- [ ] **Step 2: Extend HomeProps and the Home signature**

```typescript
interface HomeProps {
  production: ProductionInfo;
  productions: ProductionInfo[];
  onSelectProduction: (p: ProductionInfo) => void;
  onSwitchProduction: () => void;
  onIngestComplete: () => void;
  onOpenGuide: () => void;
}

function Home({ production, productions, onSelectProduction, onSwitchProduction, onIngestComplete, onOpenGuide }: HomeProps) {
```

- [ ] **Step 3: Extract the random-document handler**

Inside `Home`, above the early returns, add (this is the "I'm Feeling Lucky" logic verbatim from App.tsx:355-368, which the next step deletes):

```typescript
  const handleRandomDoc = async () => {
    try {
      const { getRandomDocument } = await import('./api/client');
      const { id } = await getRandomDocument(production.id);
      setViewDocId(id);
    } catch (e: any) {
      showToast(
        e?.message?.includes('404')
          ? 'No documents in this production yet.'
          : `Could not pick a random document: ${e?.message || 'unknown error'}`,
        'error',
      );
    }
  };
```

- [ ] **Step 4: Replace the header and search-row JSX**

Delete from `{/* Header */}` (App.tsx:313) through the end of the header `</div>` (App.tsx:345), and delete the search-bar row block (App.tsx:349-373, the `div` containing `<SearchBar …/>` and the I'm Feeling Lucky button). In their place, directly under the opening `<div style={{ minHeight: '100vh', background: 'var(--color-neutral-50)' }}>`:

```tsx
      <AppHeader
        production={production}
        productions={productions}
        onSelectProduction={onSelectProduction}
        onShowAllProductions={onSwitchProduction}
        onSearch={handleSearch}
        onLogoClick={clearSearch}
        initialQuery={searchQuery}
        onOpenReview={() => setShowAIReview(true)}
        onOpenDashboard={() => setShowDashboard(true)}
        onOpenShare={production.is_owner ? () => setShowManageAccess(true) : undefined}
        onOpenAudit={production.is_owner ? () => setShowAuditLog(true) : undefined}
        onOpenQueues={() => setShowQueueManager(true)}
        onOpenIngest={() => setShowIngestWizard(true)}
        onOpenGuide={onOpenGuide}
        onRandomDoc={handleRandomDoc}
      />
```

The content area now begins with `<TopicGroups …/>` (removed in Phase 2, keep for now).

- [ ] **Step 5: Pass the new props from AppRouter**

In `AppRouter` (App.tsx:948-955), update the Home branch:

```tsx
    content = (
      <Home
        production={activeProduction}
        productions={productions}
        onSelectProduction={setActiveProduction}
        onSwitchProduction={() => setActiveProduction(null)}
        onIngestComplete={handleIngestComplete}
        onOpenGuide={openGuide}
      />
    );
```

- [ ] **Step 6: Delete SearchBar**

```bash
git rm frontend/src/components/SearchBar.tsx
```

Confirm nothing else imports it: `cd frontend && npx eslint src/App.tsx && npm run build` — the build fails if any import remains.

- [ ] **Step 7: Verify behavior manually**

Run: `cd frontend && npm run dev` and check against a running backend:
- Header is ink-blue; production name shows in the switcher; switching productions works; "All productions…" returns to the picker.
- Typing a question mark in the omnibox flips the pill to "✦ Ask"; clicking the pill toggles it; submitting runs the right mode (check the mode label on the results header).
- Saved searches and metadata filters still work from the omnibox menus.
- ✦ Review opens Smart Review; Dashboard opens; gear menu shows Share/Audit only for the owner; Guide, Ingest, Review queues, Random document, Sign out all fire.
- Refresh with `?q=…` in the URL still restores the search; bulk-select bar and Send to AI Agent unaffected.

- [ ] **Step 8: Lint and commit**

Run: `cd frontend && npx eslint src/App.tsx` → 0 errors

```bash
git add frontend/src/App.tsx
git commit -m "feat(frontend): mount command bar in Home, retire old header and SearchBar"
```

---

### Task 6: Case-desk production picker

**Files:**
- Modify: `frontend/src/components/ProductionPicker.tsx` (full rewrite below)
- Modify: `frontend/src/styles/layout.css` (append case-desk styles)

**Interfaces:**
- Consumes: `AppHeader` (production undefined → logo + gear + avatar only), `ProductionInfo.document_count` (Task 1/2), existing `deleteProduction` API.
- Produces: same external contract as today — `<ProductionPicker productions onSelect onIngest onDeleted? />` — AppRouter (App.tsx:939-945) needs no changes.

- [ ] **Step 1: Rewrite the component**

Replace the full contents of `frontend/src/components/ProductionPicker.tsx`:

```tsx
import { useState } from 'react';
import AppHeader from './AppHeader';
import { deleteProduction } from '../api/client';
import type { ProductionInfo } from '../types';

interface Props {
  productions: ProductionInfo[];
  onSelect: (production: ProductionInfo) => void;
  onIngest: () => void;
  onDeleted?: () => void;
}

const dateFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });

export default function ProductionPicker({ productions, onSelect, onIngest, onDeleted }: Props) {
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const handleDelete = async (p: ProductionInfo) => {
    setDeletingId(p.id);
    setDeleteError(null);
    try {
      await deleteProduction(p.id);
      onDeleted?.();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setDeletingId(null);
      setConfirmId(null);
    }
  };

  return (
    <div className="case-desk">
      <AppHeader productions={productions} onOpenIngest={onIngest} />

      <div className="content-area case-desk-content">
        <h1 className="case-desk-title">Your productions</h1>
        <p className="case-desk-sub">Pick a production to continue its review.</p>
        {deleteError && <p className="case-desk-error" role="alert">{deleteError}</p>}

        <div className="case-desk-grid">
          {productions.map(p => (
            <div key={p.id} className="case-card-wrap">
              <button type="button" className="case-card card" onClick={() => onSelect(p)}>
                <div className="case-card-name">{p.name}</div>
                {p.description && <div className="case-card-desc">{p.description}</div>}
                {/* Phase 2 slot: one-line AI theme summary from the production brief. */}
                <div className="case-card-meta">
                  <span>{p.document_count.toLocaleString()} document{p.document_count === 1 ? '' : 's'}</span>
                  <span className="case-card-dot">·</span>
                  <span>added {dateFmt.format(new Date(p.created_at))}</span>
                </div>
                <div className="case-card-badges">
                  {p.is_owner
                    ? <span className="badge badge-blue">Owner</span>
                    : <span className="badge badge-gray">Shared</span>}
                </div>
              </button>

              {p.is_owner && (
                <button
                  type="button"
                  className="case-card-delete"
                  onClick={() => setConfirmId(p.id)}
                  title="Delete production"
                  aria-label={`Delete production ${p.name}`}
                >
                  ×
                </button>
              )}

              {confirmId === p.id && (
                <div className="case-card-confirm">
                  <div className="case-card-confirm-title">Delete "{p.name}"?</div>
                  <div className="case-card-confirm-body">
                    This permanently removes all documents, tags, notes, and uploaded files. Cannot be undone.
                  </div>
                  <div className="case-card-confirm-actions">
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => setConfirmId(null)}
                      disabled={deletingId === p.id}
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger btn-sm"
                      onClick={() => handleDelete(p)}
                      disabled={deletingId === p.id}
                    >
                      {deletingId === p.id ? 'Deleting…' : 'Delete'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}

          <button type="button" className="case-card case-card-new card" onClick={onIngest}>
            <div className="case-card-new-plus">＋</div>
            <div className="case-card-new-label">Ingest a production</div>
            <div className="case-card-desc">Load a new document production into Vigilist.</div>
          </button>
        </div>
      </div>
    </div>
  );
}
```

Notes: the old `alert()` on delete failure becomes an inline `role="alert"` message (spec §4: no blank/blocking states); `useAuth` is no longer imported here — AppHeader owns identity/sign-out.

- [ ] **Step 2: Append case-desk styles**

At the end of `frontend/src/styles/layout.css` add:

```css
/* ── Case Desk (production picker) ── */

.case-desk {
  min-height: 100vh;
  background: var(--color-neutral-50);
}

.case-desk-content {
  padding-top: var(--space-10);
}

.case-desk-title {
  font-family: var(--font-serif);
  font-size: var(--text-3xl);
  font-weight: var(--font-semibold);
  color: var(--color-primary-900);
  text-align: center;
  margin-bottom: var(--space-1);
}

.case-desk-sub {
  text-align: center;
  color: var(--color-neutral-500);
  font-size: var(--text-sm);
  margin-bottom: var(--space-8);
}

.case-desk-error {
  text-align: center;
  color: var(--color-error);
  font-size: var(--text-sm);
  margin-bottom: var(--space-4);
}

.case-desk-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: var(--space-4);
  max-width: 980px;
  margin: 0 auto;
}

.case-card-wrap {
  position: relative;
}

.case-card {
  display: block;
  width: 100%;
  height: 100%;
  padding: var(--space-5);
  cursor: pointer;
  background: var(--color-card);
  border: 1px solid var(--color-neutral-100);
  text-align: left;
  font: inherit;
  color: inherit;
  transition: box-shadow var(--transition-slow), transform var(--transition-slow);
}
.case-card:hover,
.case-card:focus-visible {
  box-shadow: var(--shadow-lg);
  transform: translateY(-2px);
  outline: none;
}

.case-card-name {
  font-family: var(--font-serif);
  font-size: var(--text-xl);
  font-weight: var(--font-semibold);
  color: var(--color-primary-900);
  margin-bottom: var(--space-2);
  padding-right: var(--space-6);
}

.case-card-desc {
  font-size: var(--text-sm);
  color: var(--color-neutral-500);
  margin-bottom: var(--space-3);
  line-height: var(--leading-relaxed);
}

.case-card-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-neutral-400);
  margin-bottom: var(--space-3);
}

.case-card-dot {
  opacity: 0.5;
}

.case-card-badges {
  display: flex;
  gap: var(--space-2);
}

.case-card-delete {
  position: absolute;
  top: var(--space-2);
  right: var(--space-2);
  background: transparent;
  border: none;
  color: var(--color-neutral-300);
  cursor: pointer;
  font-size: var(--text-lg);
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
  line-height: 1;
  z-index: 1;
  transition: color var(--transition-fast);
}
.case-card-delete:hover {
  color: var(--color-error);
}

.case-card-confirm {
  position: absolute;
  inset: 0;
  background: rgba(255, 255, 255, 0.97);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  gap: var(--space-3);
  z-index: 2;
}

.case-card-confirm-title {
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
  color: var(--color-ink);
  text-align: center;
}

.case-card-confirm-body {
  font-size: var(--text-xs);
  color: var(--color-neutral-500);
  text-align: center;
}

.case-card-confirm-actions {
  display: flex;
  gap: var(--space-2);
}

.case-card-new {
  border-style: dashed;
  border-color: var(--color-neutral-300);
  background: transparent;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-1);
}
.case-card-new:hover {
  border-color: var(--color-ink);
}

.case-card-new-plus {
  font-size: var(--text-3xl);
  color: var(--color-ink-light);
  line-height: 1;
}

.case-card-new-label {
  font-family: var(--font-serif);
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
  color: var(--color-primary-900);
}
```

- [ ] **Step 3: Verify behavior manually**

Run: `cd frontend && npm run dev` (with the Task 1 backend running so `document_count` is real):
- With 2+ productions (or after deselecting via "All productions…"), the case desk shows cards with doc counts and added dates, the Owner/Shared badge, and the dashed "＋ Ingest a production" card.
- Delete flow: × → confirm overlay → Cancel and Delete both work; a failed delete shows the inline error, not an `alert()`.
- Header on the picker shows only logo, gear (Ingest / Sign out), avatar.

- [ ] **Step 4: Lint, build, commit**

Run: `cd frontend && npx eslint src/components/ProductionPicker.tsx` → 0 errors
Run: `cd frontend && npm run build` → succeeds

```bash
git add frontend/src/components/ProductionPicker.tsx frontend/src/styles/layout.css
git commit -m "feat(frontend): case-desk production picker with document counts"
```

---

### Task 7: Phase verification sweep

**Files:**
- No planned changes — fixes only if verification fails.

- [ ] **Step 1: Full builds**

Run: `cd frontend && npm run build` → succeeds
Run: `cd backend && python -m pytest tests/ -v` → same pass/fail set as the Task 1 baseline

- [ ] **Step 2: Lint the phase's touched files**

Run:
```bash
cd frontend && npx eslint src/App.tsx src/types/index.ts src/utils/searchMode.ts src/components/Omnibox.tsx src/components/AppHeader.tsx src/components/ProductionPicker.tsx
```
Expected: 0 errors.

- [ ] **Step 3: End-to-end manual pass (dev server + backend)**

Walk the full loop once as an owner and once as a non-owner account:
1. Sign in → case desk (or straight to Home with one production).
2. Switch productions via the header switcher and via "All productions…".
3. Search full-text, then ask a question (pill shows ✦ Ask), toggle the mode pill, save a search, apply a metadata filter.
4. Open ✦ Review, Dashboard, and every gear item; verify owner-gating of Share/Audit.
5. Open a document from results (URL updates), refresh the page, confirm state restores.
6. Select several documents → bulk bar → Send to AI Agent still opens the chat panel with chips.
7. Onboarding: gear → Guide opens the guide. (Its copy still describes the old header — that rewrite is Phase 5, tracked in the spec.)

- [ ] **Step 4: Commit any fixes**

```bash
git add -A && git commit -m "fix(frontend): phase 1 verification fixes"
```
(Skip if nothing changed.)

---

## Self-Review Notes

- **Spec coverage (Phase 1 slice):** command-bar header ✔ (Task 4/5), omnibox with visible mode ✔ (Task 3), gear menu absorbing Share/Ingest/Audit/Guide ✔ (Task 4/5), case-desk picker ✔ (Task 6 — theme summary/pipeline status deliberately deferred to Phase 2 per spec), brass token ✔ (Task 2), inline-style cleanup on touched surfaces ✔ (Tasks 5/6 replace the header/picker inline styles; other screens are Phase 5), no router ✔, telemetry/AI pipeline are later phases ✔.
- **Known interim states:** "Review queues" and "Random document" live in the gear menu until Phase 4; TopicGroups strip and old `.app-header` / `.production-*` CSS remain for untouched screens until Phases 2/5; the guide's copy lags until Phase 5.
- **Type consistency:** `onSearch(query, metadata?, forceMode?)` matches Home's `handleSearch` exactly across Tasks 2→3→4→5; `document_count` is non-optional in TS and always emitted by the API after Task 1 (deploy backend first).
