# Phase 3 "Context Rail" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A docked right-hand Intelligence rail on Home that follows the reviewer's context (production → one document → multi-select), absorbs the AI chat (the FAB and full-screen overlay die), takes over "Send to AI Agent" from the bulk bar, and adds a theme-chip column to the document list.

**Architecture:** Chat state is extracted from `AIAgent.tsx` into a `useChat` hook owned by Home, so the omnibox and rail can both drive one conversation; a presentational `ChatPanel` renders transcript+composer inside the new `ContextRail`, which switches between three selection-driven states. Home's content area becomes the left column of a flex shell; the rail is in-flow at `--rail-width: 380px`, collapsible (state lifted to Home so the omnibox "Ask AI" can force-expand it), and turns into an edge-tab drawer below 1024px. The backend list endpoint is enriched with each document's cluster id/label (batch query mirroring the existing note-count pattern) to power theme chips.

**Tech Stack:** React 19 + TS + plain token CSS; FastAPI + async SQLAlchemy; existing `/api/ai/chat` SSE (unchanged — chat scope remains attached doc_ids).

**Spec:** `docs/superpowers/specs/2026-07-16-ui-redesign-ambient-ai-design.md` §3 (context rail), §1 (omnibox → chat; FAB deletion), §3-Home (list AI columns — theme chip now; relevance markers are Phase 4).

## Global Constraints

- No new dependencies; no router; no frontend test framework. Backend tests: `cd backend && python -m pytest tests/ -v`; known pre-existing failure `test_ai_review.py::test_build_classification_prompt` stays untouched.
- Touched frontend files: `npx eslint <file>` → 0 errors, NO eslint-disable, no setState-in-effect prop-sync (strict React Compiler rules); `npm run build` green after every frontend task.
- No hardcoded colors in TSX; theme colors via `var(--theme-N)` (N = (index % 8) + 1 over the production's cluster list order, same mapping ProductionBrief uses); ✦ is the AI mark.
- Chat behavior preserved exactly: session persistence while Home is mounted (state lives in Home's `useChat`, so it resets on production switch — same as today's remount semantics), streaming with Stop keeping partial text, copy/download/clear transcript, attachment chips removable.
- `streamChat(messages, docIds, handlers, signal)` signature unchanged; `/api/ai/chat` backend untouched.
- Bulk bar keeps Download / Tag / Clear; "Send to AI Agent" leaves it. URL-state keys unchanged.
- DocumentViewer is untouched this phase (its AI Tools fold into the sidebar in Phase 5).
- z-order reality: floating bar 100, old FAB 150 (dying), old overlay 250 (dying). The drawer variant of the rail uses z-index 200; the mobile edge tab 150.
- Commit after every task with the message given in the task.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `backend/app/routers/documents.py:40-162` | modify | enrich `list_documents` with `cluster_id`/`cluster_label` per doc |
| `backend/app/schemas.py` | modify | two fields on the list-row schema |
| `backend/tests/test_cluster_label_map.py` | create | micro-test for the pure row→map helper |
| `frontend/src/types/index.ts` | modify | `AttachedDoc` moves here; `DocumentSummary.cluster_id/cluster_label` |
| `frontend/src/hooks/useChat.ts` | create | chat state machine extracted from AIAgent (messages/stream/attach/send/stop/clear) |
| `frontend/src/components/ChatPanel.tsx` | create | presentational transcript + chips + composer + header actions |
| `frontend/src/components/ContextRail.tsx` | create | rail shell: collapse, drawer mode, three context states |
| `frontend/src/components/AIAgent.tsx` | **delete** | superseded by useChat + ChatPanel + ContextRail |
| `frontend/src/components/Omnibox.tsx` | modify | "Ask AI" affordance when mode is semantic and `onAsk` provided |
| `frontend/src/components/AppHeader.tsx` | modify | pass-through `onAsk` prop to Omnibox |
| `frontend/src/App.tsx` | modify | flex shell + rail mount; remove FAB/overlay; bulk bar trim; theme column in list+grid |
| `frontend/src/styles/variables.css` | modify | `--rail-width: 380px` |
| `frontend/src/styles/components.css` | modify | `.context-rail*`, `.chat-*` classes; retire `.ai-agent-overlay/-panel/-fab` sizing blocks (chat message/composer classes are reused) |
| `frontend/src/styles/layout.css` | modify | `.home-shell` flex, floating-bar offset, <1024px drawer breakpoint |

**Phase notes:** relevance markers in the list and DocumentViewer sidebar changes are Phases 4-5; the rail's single-doc state shows summary/find-similar/ask only.

---

### Task 1: Backend — cluster fields on the document listing

**Files:**
- Modify: `backend/app/routers/documents.py:40-162` (`list_documents`)
- Modify: `backend/app/schemas.py` (list-row schema — find the Pydantic model that `list_documents` builds at documents.py:143-156, likely `DocumentSummaryOut` or similar; read it first)
- Test: `backend/tests/test_cluster_label_map.py` (create)

**Interfaces:**
- Produces: each document row in `GET /api/documents` gains `cluster_id: int | None = None` and `cluster_label: str | None = None`. Pure helper `cluster_label_map(rows) -> dict[str, dict]` in documents.py where `rows` is an iterable of `(document_id, cluster_id, label)` tuples → `{str(document_id): {"cluster_id": id, "cluster_label": label}}`. Frontend Task 2 mirrors the two fields.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_cluster_label_map.py`:

```python
"""cluster_label_map: rows -> per-document cluster info for list enrichment."""

import uuid

from app.routers.documents import cluster_label_map


def test_maps_rows_and_stringifies_ids():
    d1, d2 = uuid.uuid4(), uuid.uuid4()
    out = cluster_label_map([(d1, 7, "Recall timeline"), (d2, 9, None)])
    assert out[str(d1)] == {"cluster_id": 7, "cluster_label": "Recall timeline"}
    assert out[str(d2)] == {"cluster_id": 9, "cluster_label": None}


def test_empty_rows_give_empty_map():
    assert cluster_label_map([]) == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && python -m pytest tests/test_cluster_label_map.py -v`
Expected: FAIL — `ImportError: cannot import name 'cluster_label_map'`.

- [ ] **Step 3: Implement**

In `backend/app/routers/documents.py`, add near the other helpers:

```python
def cluster_label_map(rows) -> dict[str, dict]:
    """(document_id, cluster_id, label) tuples -> {doc_id_str: {cluster_id, cluster_label}}."""
    return {
        str(doc_id): {"cluster_id": cluster_id, "cluster_label": label}
        for doc_id, cluster_id, label in rows
    }
```

In `list_documents`, after the existing note/annotation batch queries (documents.py:121-139), add one more batch query over the page's `doc_ids` (mirror that pattern exactly — imports `DocumentClusterAssignment`, `DocumentCluster` from `app.models`):

```python
    cluster_rows = (
        await db.execute(
            select(
                DocumentClusterAssignment.document_id,
                DocumentClusterAssignment.cluster_id,
                DocumentCluster.label,
            )
            .join(DocumentCluster, DocumentCluster.id == DocumentClusterAssignment.cluster_id)
            .where(DocumentClusterAssignment.document_id.in_(doc_ids))
        )
    ).all()
    clusters_by_doc = cluster_label_map(cluster_rows)
```

and in the row construction (documents.py:143-156) add:

```python
            cluster_id=(clusters_by_doc.get(str(doc.id)) or {}).get("cluster_id"),
            cluster_label=(clusters_by_doc.get(str(doc.id)) or {}).get("cluster_label"),
```

In `backend/app/schemas.py`, on the list-row model that construction uses, add:

```python
    cluster_id: int | None = None
    cluster_label: str | None = None
```

(Adapt names to the actual model/variable names found in the file — the pattern, fields, and helper are binding; local names are not.)

- [ ] **Step 4: Run tests**

Run: `cd backend && python -m pytest tests/test_cluster_label_map.py tests/ -v`
Expected: 2 new pass; suite otherwise unchanged (62+ passed, 1 known pre-existing failure). Then `python -c "from app.main import app"` → exit 0.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/documents.py backend/app/schemas.py backend/tests/test_cluster_label_map.py
git commit -m "feat(api): cluster id/label on document listing rows"
```

---

### Task 2: Frontend groundwork — types, token, `useChat` hook

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/styles/variables.css` (Layout block)
- Create: `frontend/src/hooks/useChat.ts`

**Interfaces:**
- Produces (binding for Tasks 3-6):

```typescript
// types/index.ts — AttachedDoc moves here from AIAgent.tsx
export interface AttachedDoc {
  id: string;
  label: string;
}
```
Also add to `DocumentSummary`: `cluster_id?: number | null; cluster_label?: string | null;`

```typescript
// hooks/useChat.ts
import type { ChatMessage } from '../api/client';   // exported there already (client.ts:229)
export interface ChatState {
  messages: ChatMessage[];
  streaming: boolean;
  streamingText: string;
  attachedDocs: AttachedDoc[];
  attachDocs: (docs: AttachedDoc[]) => void;   // merge, dedup by id
  removeDoc: (id: string) => void;
  send: (text: string) => void;                // no-op if streaming or text blank
  stop: () => void;                            // abort; keep partial text as assistant turn
  clear: () => void;                           // abort + wipe messages/attachments
  transcriptText: () => string;                // "You:"/"AI Agent:" formatted
}
export function useChat(): ChatState
```

Token: `--rail-width: 380px;` in variables.css's Layout block.

- [ ] **Step 1: Make the type + token edits** (code above, verbatim).

- [ ] **Step 2: Write the hook**

Port the logic from `frontend/src/components/AIAgent.tsx` lines 17-131 verbatim where possible — this is an extraction, not a rewrite. Complete file:

```typescript
import { useCallback, useRef, useState } from 'react';
import { streamChat, type ChatMessage } from '../api/client';
import type { AttachedDoc } from '../types';

export interface ChatState {
  messages: ChatMessage[];
  streaming: boolean;
  streamingText: string;
  attachedDocs: AttachedDoc[];
  attachDocs: (docs: AttachedDoc[]) => void;
  removeDoc: (id: string) => void;
  send: (text: string) => void;
  stop: () => void;
  clear: () => void;
  transcriptText: () => string;
}

function formatTranscript(messages: ChatMessage[]): string {
  return messages
    .map(m => `${m.role === 'user' ? 'You' : 'AI Agent'}:\n${m.content}`)
    .join('\n\n');
}

/**
 * The AI chat state machine, extracted from the retired AIAgent overlay so
 * the context rail and the omnibox can share one conversation. Owned by Home:
 * the conversation lives as long as the production view does.
 */
export function useChat(): ChatState {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [attachedDocs, setAttachedDocs] = useState<AttachedDoc[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const attachDocs = useCallback((docs: AttachedDoc[]) => {
    setAttachedDocs(prev => {
      const seen = new Set(prev.map(d => d.id));
      return [...prev, ...docs.filter(d => !seen.has(d.id))];
    });
  }, []);

  const removeDoc = useCallback((id: string) => {
    setAttachedDocs(prev => prev.filter(d => d.id !== id));
  }, []);

  const send = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed || streaming) return;
    setMessages(prev => {
      const next: ChatMessage[] = [...prev, { role: 'user', content: trimmed }];
      const controller = new AbortController();
      abortRef.current = controller;
      setStreaming(true);
      setStreamingText('');
      let acc = '';
      streamChat(
        next,
        attachedDocs.map(d => d.id),
        {
          onDelta: t => { acc += t; setStreamingText(acc); },
          onError: message => {
            acc += (acc ? '\n\n' : '') + `⚠ ${message}`;
            setStreamingText(acc);
          },
        },
        controller.signal,
      )
        .catch(() => { /* aborted or network drop — partial text is kept below */ })
        .finally(() => {
          setStreaming(false);
          setStreamingText('');
          if (acc) setMessages(curr => [...curr, { role: 'assistant', content: acc }]);
          abortRef.current = null;
        });
      return next;
    });
  }, [attachedDocs, streaming]);

  const stop = useCallback(() => { abortRef.current?.abort(); }, []);

  const clear = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setStreamingText('');
    setAttachedDocs([]);
  }, []);

  const transcriptText = useCallback(() => formatTranscript(messages), [messages]);

  return { messages, streaming, streamingText, attachedDocs, attachDocs, removeDoc, send, stop, clear, transcriptText };
}
```

**Porting caution:** compare against the real AIAgent `send`/`stop` (AIAgent.tsx:50-94) and preserve its exact semantics for partial-text-on-stop and error rendering; if the original threads `acc` differently (e.g. commits partial text inside `stop`), match the original. The `setMessages(prev => …)` wrapper returning `next` is a refactor of the original's local variable — keep the behavior identical: user message appears immediately, assistant message commits once at stream end.

- [ ] **Step 3: Verify**

Run: `cd frontend && npx eslint src/hooks/useChat.ts src/types/index.ts` → 0 errors
Run: `cd frontend && npm run build` → succeeds (hook unused so far; AIAgent.tsx still compiles with its local AttachedDoc — if TS complains about the duplicate export name, have AIAgent.tsx re-export from types: `export type { AttachedDoc } from '../types';` and delete its local interface).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/styles/variables.css frontend/src/hooks/useChat.ts frontend/src/components/AIAgent.tsx
git commit -m "feat(frontend): useChat hook, AttachedDoc type move, rail width token"
```

---

### Task 3: ChatPanel component

**Files:**
- Create: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/styles/components.css` (append `.chat-*` classes)

**Interfaces:**
- Consumes: `ChatState` (Task 2).
- Produces: `<ChatPanel chat={ChatState} placeholder={string} autoFocusToken={number} />` — presentational transcript + attachment chips + composer + header actions (Copy/Download/Clear, Stop while streaming). `autoFocusToken`: increment to focus the composer (used by "Ask about this document"); 0 = no focus.

- [ ] **Step 1: Write the component**

Port the JSX from AIAgent.tsx lines 138-230 (transcript list, chips row, typing indicator, composer) into a docked-friendly layout. Complete file — reuse the existing message CSS classes so the visual language carries over:

```tsx
import { useEffect, useRef } from 'react';
import type { ChatState } from '../hooks/useChat';

interface Props {
  chat: ChatState;
  placeholder: string;
  autoFocusToken: number;
}

export default function ChatPanel({ chat, placeholder, autoFocusToken }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat.messages, chat.streamingText]);

  useEffect(() => {
    if (autoFocusToken > 0) inputRef.current?.focus();
  }, [autoFocusToken]);

  const submit = () => {
    const el = inputRef.current;
    if (!el) return;
    chat.send(el.value);
    el.value = '';
  };

  const download = () => {
    const blob = new Blob([chat.transcriptText()], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vigilist-ai-chat-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <div className="chat-panel">
      {chat.messages.length > 0 && (
        <div className="chat-actions">
          <button type="button" className="btn btn-ghost btn-xs" onClick={() => navigator.clipboard.writeText(chat.transcriptText())}>Copy</button>
          <button type="button" className="btn btn-ghost btn-xs" onClick={download}>Download</button>
          <button type="button" className="btn btn-ghost btn-xs" onClick={chat.clear}>Clear</button>
        </div>
      )}

      {chat.attachedDocs.length > 0 && (
        <div className="ai-agent-docs chat-docs">
          <span className="chat-docs-label">Context:</span>
          {chat.attachedDocs.map(d => (
            <span key={d.id} className="ai-agent-doc-chip">
              {d.label}
              <button type="button" onClick={() => chat.removeDoc(d.id)} aria-label={`Remove ${d.label}`}>×</button>
            </span>
          ))}
        </div>
      )}

      <div className="chat-body" ref={scrollRef}>
        {chat.messages.length === 0 && !chat.streaming && (
          <div className="chat-empty">
            <span className="brief-ai-mark">✦</span> {placeholder}
          </div>
        )}
        {chat.messages.map((m, i) => (
          <div key={i} className={`ai-agent-msg ai-agent-msg-${m.role === 'user' ? 'user' : 'assistant'}`}>
            <div className="ai-agent-msg-role">{m.role === 'user' ? 'You' : '✦ AI'}</div>
            <div className="ai-agent-msg-content">{m.content}</div>
          </div>
        ))}
        {chat.streaming && (
          <div className="ai-agent-msg ai-agent-msg-assistant">
            <div className="ai-agent-msg-role">✦ AI</div>
            <div className="ai-agent-msg-content">
              {chat.streamingText || (
                <span className="ai-agent-typing"><span /><span /><span /></span>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="chat-composer">
        <textarea
          ref={inputRef}
          className="chat-input"
          rows={2}
          placeholder={placeholder}
          aria-label="Ask the AI"
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
          }}
        />
        {chat.streaming ? (
          <button type="button" className="btn btn-secondary btn-sm" onClick={chat.stop}>Stop</button>
        ) : (
          <button type="button" className="btn btn-primary btn-sm" onClick={submit}>Send</button>
        )}
      </div>
    </div>
  );
}
```

Note the composer is **uncontrolled** (ref-based) deliberately — no keystroke re-renders of the whole rail. If the original AIAgent composer had additional behavior (e.g. Shift+Enter newline is implicit here), preserve it.

- [ ] **Step 2: Append CSS**

In `frontend/src/styles/components.css`, after the existing `.ai-agent-*` block:

```css
/* ── Chat panel (context rail) ── */

.chat-panel {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.chat-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-2);
}

.chat-docs {
  border-top: 1px solid var(--color-neutral-100);
}

.chat-docs-label {
  font-size: var(--text-xs);
  color: var(--color-neutral-400);
}

.chat-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.chat-empty {
  color: var(--color-neutral-400);
  font-size: var(--text-sm);
  text-align: center;
  margin-top: var(--space-6);
}

.chat-composer {
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
  padding: var(--space-2);
  border-top: 1px solid var(--color-neutral-200);
}

.chat-input {
  flex: 1;
  resize: none;
  border: 1px solid var(--color-neutral-200);
  border-radius: var(--radius-md);
  padding: var(--space-2);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  background: var(--color-neutral-0);
  color: var(--color-neutral-700);
}
.chat-input:focus {
  outline: none;
  box-shadow: var(--shadow-ring);
}
```

- [ ] **Step 3: Verify**

`cd frontend && npx eslint src/components/ChatPanel.tsx` → 0 errors; `npm run build` → succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ChatPanel.tsx frontend/src/styles/components.css
git commit -m "feat(frontend): ChatPanel presentational component"
```

---

### Task 4: ContextRail component + rail CSS

**Files:**
- Create: `frontend/src/components/ContextRail.tsx`
- Modify: `frontend/src/styles/layout.css` (append rail layout + breakpoint)
- Modify: `frontend/src/styles/components.css` (append rail content classes)

**Interfaces:**
- Consumes: `ChatState`, `ChatPanel`, client fns `getDocument`, `summarizeDocument`, `findSimilar`, `getPipeline`; types `DocumentSummary`, `SearchResult`, `ProductionInfo`, `AttachedDoc`.
- Produces:

```tsx
interface ContextRailProps {
  production: ProductionInfo;
  chat: ChatState;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  autoFocusToken: number;                 // forwarded to ChatPanel
  selectedIds: Set<string>;
  documents: DocumentSummary[];
  searchResults: SearchResult[];
  onViewDocument: (id: string) => void;
  onSimilarResults: (label: string, results: SearchResult[]) => void;
}
```

Behavior contract:
- **Collapsed:** renders only a slim vertical tab (`.context-rail-tab`, in-flow, ~36px wide, full height) with a ✦ and "Intelligence" written vertically; clicking calls `onToggleCollapsed`.
- **Expanded, 0 selected (production state):** header "✦ Intelligence" + collapse button; a one-line production context (fetch `getPipeline(production.id)` ONCE on mount — no polling; if `brief.overview` exists show its first sentence in `.rail-context-line`, else the production name); then ChatPanel (placeholder "Ask the production…").
- **Expanded, 1 selected (document state):** doc header (title or bates, from `documents`/`searchResults` lookup); summary block — fetch `getDocument(id)` lazily per selected id (cache in a ref map); if `summary` present show it (`.rail-summary`), else a "✦ Summarize" button calling `summarizeDocument(id)` then showing the result (busy state while pending); action row: "Find similar" → `findSimilar(id)` → `onSimilarResults(`Similar to ${label}`, res.results)` (toast on error), "Open document" → `onViewDocument(id)`, "✦ Ask about this document" → `chat.attachDocs([{id, label}])` (parent bumps focus token — see Task 5); ChatPanel below.
- **Expanded, >1 selected (multi state):** "N documents selected" line; "✦ Ask about these N" → `chat.attachDocs(all selected as AttachedDoc[])`; ChatPanel below. (Tagging stays in the bulk bar.)
- Labels resolve exactly like the old `sendSelectionToAgent`: bates from searchResults first, then documents, else `id.slice(0,8)`.
- All fetches guarded with cancellation flags; errors → `showToast(..., 'error')` (summarize/find-similar), silent console.warn for the pipeline line.
- No polling, no timers.

- [ ] **Step 1: Write the component.** The implementer writes the complete file satisfying the contract above. Skeleton for the state plumbing (complete the render branches per the contract):

```tsx
import { useCallback, useEffect, useRef, useState } from 'react';
import ChatPanel from './ChatPanel';
import { findSimilar, getDocument, getPipeline, summarizeDocument } from '../api/client';
import { showToast } from './Toast';
import type { ChatState } from '../hooks/useChat';
import type { AttachedDoc, DocumentSummary, ProductionInfo, SearchResult } from '../types';

// ...ContextRailProps as specified...

export default function ContextRail({ production, chat, collapsed, onToggleCollapsed, autoFocusToken, selectedIds, documents, searchResults, onViewDocument, onSimilarResults }: ContextRailProps) {
  const [contextLine, setContextLine] = useState<string | null>(null);
  const [docSummaries, setDocSummaries] = useState<Record<string, string | null>>({});
  const [summarizing, setSummarizing] = useState(false);
  const [findingSimilar, setFindingSimilar] = useState(false);
  const fetchedDocIds = useRef(new Set<string>());
  // mount-once pipeline fetch for the context line; lazy getDocument per selected id;
  // labelFor() identical to the old sendSelectionToAgent's resolution order.
  // Render: collapsed tab | production / document / multi states per contract.
}
```

- [ ] **Step 2: Rail layout CSS** — append to `frontend/src/styles/layout.css`:

```css
/* ── Home shell + context rail ── */

.home-shell {
  display: flex;
  align-items: stretch;
  gap: 0;
}

.home-shell > .content-area {
  flex: 1;
  min-width: 0;
}

.context-rail {
  width: var(--rail-width);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-card);
  border-left: 1px solid var(--color-neutral-200);
  position: sticky;
  top: var(--cb-height);
  height: calc(100vh - var(--cb-height));
  overflow: hidden;
}

.context-rail-tab {
  width: 36px;
  flex-shrink: 0;
  border: none;
  border-left: 1px solid var(--color-neutral-200);
  background: var(--color-card);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: var(--space-4);
  gap: var(--space-2);
  position: sticky;
  top: var(--cb-height);
  height: calc(100vh - var(--cb-height));
  color: var(--color-neutral-400);
  transition: color var(--transition-fast);
}
.context-rail-tab:hover {
  color: var(--color-ink);
}

.context-rail-tab .rail-tab-label {
  writing-mode: vertical-rl;
  font-size: var(--text-xs);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

/* Rail pushes the centered floating bar leftward so it stays centered over the list. */
.home-shell.rail-open ~ .floating-bar,
.home-shell.rail-open .floating-bar {
  left: calc(50% - var(--rail-width) / 2);
}

@media (max-width: 1024px) {
  /* Below tablet width the rail leaves the flow entirely; Phase 5 may add a
     drawer. The chat remains reachable by widening the window — documented
     limitation for this phase. */
  .context-rail,
  .context-rail-tab {
    display: none;
  }
}
```

- [ ] **Step 3: Rail content CSS** — append to `frontend/src/styles/components.css`:

```css
/* ── Context rail content ── */

.rail-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
  border-bottom: 1px solid var(--color-neutral-100);
}

.rail-title {
  font-family: var(--font-serif);
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
  color: var(--color-primary-900);
  flex: 1;
}

.rail-context-line {
  font-size: var(--text-xs);
  color: var(--color-neutral-500);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-neutral-100);
  line-height: var(--leading-relaxed);
}

.rail-doc-header {
  padding: var(--space-3);
  border-bottom: 1px solid var(--color-neutral-100);
}

.rail-doc-title {
  font-weight: var(--font-semibold);
  font-size: var(--text-sm);
  color: var(--color-ink);
}

.rail-doc-bates {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--color-neutral-400);
  margin-top: var(--space-1);
}

.rail-summary {
  font-size: var(--text-sm);
  color: var(--color-neutral-600);
  line-height: var(--leading-relaxed);
  padding: var(--space-3);
  border-bottom: 1px solid var(--color-neutral-100);
  max-height: 200px;
  overflow-y: auto;
}

.rail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-neutral-100);
}

.rail-multi-line {
  padding: var(--space-3);
  font-size: var(--text-sm);
  color: var(--color-neutral-600);
  border-bottom: 1px solid var(--color-neutral-100);
}
```

- [ ] **Step 4: Verify**

`cd frontend && npx eslint src/components/ContextRail.tsx` → 0 errors; `npm run build` → succeeds (not yet mounted).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ContextRail.tsx frontend/src/styles/layout.css frontend/src/styles/components.css
git commit -m "feat(frontend): ContextRail component with selection-driven states"
```

---

### Task 5: Home integration — mount rail, kill FAB/overlay, trim bulk bar

**Files:**
- Modify: `frontend/src/App.tsx`
- Delete: `frontend/src/components/AIAgent.tsx`
- Modify: `frontend/src/styles/components.css` (delete `.ai-agent-overlay`, `.ai-agent-panel`, `.ai-agent-header`, `.ai-agent-fab` blocks + their mobile overrides; KEEP `.ai-agent-docs/-doc-chip/-body/-empty/-msg*/-typing` which ChatPanel reuses — verify each with grep before deleting)

**Interfaces:**
- Consumes: `useChat`, `ContextRail` (Tasks 2/4).
- Produces: Home renders `<div className={home-shell${railCollapsed ? '' : ' rail-open'}}>` wrapping the existing `.content-area` and the rail; `railCollapsed` persisted at `vigilist.rail.collapsed` (plain key, not per-production); `askFocusToken` bumped whenever chat docs are attached from the rail or a question arrives from the omnibox.

- [ ] **Step 1: Home state changes** (in `Home`):

Remove: `chatOpen`, `chatDocs` state (App.tsx:79-80), `sendSelectionToAgent` (83-97), the FAB JSX (835-845), the `<AIAgent>` mount (848-853) and its import.

Add:

```tsx
import { useChat } from './hooks/useChat';
import ContextRail from './components/ContextRail';
```

```tsx
  const chat = useChat();
  const [railCollapsed, setRailCollapsed] = useState(() => {
    try { return window.localStorage.getItem('vigilist.rail.collapsed') === '1'; } catch { return false; }
  });
  const toggleRail = useCallback(() => {
    setRailCollapsed(prev => {
      const next = !prev;
      try { window.localStorage.setItem('vigilist.rail.collapsed', next ? '1' : '0'); } catch { /* storage unavailable */ }
      return next;
    });
  }, []);
  const [askFocusToken, setAskFocusToken] = useState(0);
  const focusChat = useCallback(() => {
    setRailCollapsed(prev => {
      if (prev) {
        try { window.localStorage.setItem('vigilist.rail.collapsed', '0'); } catch { /* storage unavailable */ }
      }
      return false;
    });
    setAskFocusToken(t => t + 1);
  }, []);
```

- [ ] **Step 2: Layout re-wrap.** The Home return currently renders `<AppHeader/>` then `<div className="content-area" …>` (App.tsx:342). Wrap:

```tsx
      <AppHeader ... onAsk={handleAsk} />   {/* handleAsk added in Task 7; omit the prop until then */}
      <div className={`home-shell${railCollapsed ? '' : ' rail-open'}`}>
        <div className="content-area" style={{ paddingTop: 'var(--space-4)', paddingBottom: 'var(--space-8)' }}>
          {/* existing content unchanged: ProductionBrief, list, pagination, empty states */}
        </div>
        <ContextRail
          production={production}
          chat={chat}
          collapsed={railCollapsed}
          onToggleCollapsed={toggleRail}
          autoFocusToken={askFocusToken}
          selectedIds={selectedIds}
          documents={documents}
          searchResults={searchResults}
          onViewDocument={setViewDocId}
          onSimilarResults={(label, results) => {
            setSearchQuery(label);
            setHasSearched(true);
            setSearchResults(results);
            setSearchTotal(results.length);
          }}
        />
      </div>
```

ContextRail's attach actions must also focus the chat: pass `onAttached={focusChat}` — ADD `onAttached?: () => void` to `ContextRailProps` (Task 4's contract gains this one optional callback; rail calls it after every `chat.attachDocs(...)` it initiates).

- [ ] **Step 3: Bulk bar trim.** In the floating bar (App.tsx:750-833): delete the "Send to AI Agent" button block (763-770). Download/Tag/Clear stay.

- [ ] **Step 4: Delete the overlay.**

```bash
git rm frontend/src/components/AIAgent.tsx
```
Then in components.css delete the CSS blocks for `.ai-agent-overlay` (771), `.ai-agent-panel` (782 + mobile 954), `.ai-agent-header` (796), `.ai-agent-fab` (744 + mobile 961). Before deleting each, `grep -rn "<class>" frontend/src` — delete only if the only hits are the CSS itself. The message/chip/typing classes stay (ChatPanel uses them).

- [ ] **Step 5: Verify**

`cd frontend && grep -rn "AIAgent\|ai-agent-fab\|ai-agent-overlay\|ai-agent-panel\|chatOpen\|sendSelectionToAgent" src` → no matches (ai-agent message/chip classes will still match in ChatPanel/components.css — that's expected; scope the grep accordingly).
`npx eslint src/App.tsx` → 0 errors; `npm run build` → succeeds.
Manual (controller): rail renders in all three states; chat streams; Stop/Copy/Download/Clear work; collapse persists across reload; bulk bar has no AI button; no FAB anywhere.

- [ ] **Step 6: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): dock chat in context rail; retire AI Agent overlay and FAB"
```

---

### Task 6: Theme chips in the document list and grid

**Files:**
- Modify: `frontend/src/App.tsx` (list `<th>`/`<td>` + grid meta)
- Modify: `frontend/src/api/client.ts` only if the list response needs no change (it doesn't — fields come from Task 1 via existing `listDocuments`)
- Modify: `frontend/src/styles/components.css` (a `.doc-theme-chip` class)

**Interfaces:**
- Consumes: `DocumentSummary.cluster_id/cluster_label` (Tasks 1-2), `clusters: ClusterInfo[]` state in Home, `filterClusterId`/`setFilterClusterId`.
- Produces: a Theme column in list view between Type and Pages; a theme chip in grid-card meta. Chip background = `var(--theme-N)` where N = `(clusters.findIndex(c => c.id === cluster_id) % 8) + 1` (matching ProductionBrief's ordering); unknown/missing cluster → no chip. Clicking a chip toggles `setFilterClusterId(cluster_id)` (stopPropagation so the row doesn't open the viewer).

- [ ] **Step 1: Add a lookup + chip renderer** in `Home` (above the return):

```tsx
  const themeIndexById = useMemo(() => {
    const m = new Map<number, number>();
    clusters.forEach((c, i) => m.set(c.id, (i % 8) + 1));
    return m;
  }, [clusters]);

  const themeChip = (d: DocumentSummary) => {
    if (d.cluster_id == null || !themeIndexById.has(d.cluster_id)) return null;
    const active = filterClusterId === d.cluster_id;
    return (
      <button
        type="button"
        className={`doc-theme-chip${active ? ' is-active' : ''}`}
        style={{ background: `var(--theme-${themeIndexById.get(d.cluster_id)})` }}
        onClick={e => { e.stopPropagation(); setFilterClusterId(active ? null : d.cluster_id!); }}
        title={d.cluster_label ?? 'Theme'}
      >
        {d.cluster_label ?? 'Theme'}
      </button>
    );
  };
```

- [ ] **Step 2: List view.** Add `<th>Theme</th>` after the Type header (App.tsx:540) and a matching cell after the Type `<td>`:

```tsx
                        <td className="meta-cell">{themeChip(d)}</td>
```

- [ ] **Step 3: Grid view.** Inside `.doc-grid-meta` (App.tsx:634-640), append `{themeChip(d)}` after the tag badges.

- [ ] **Step 4: CSS** — append to components.css:

```css
/* ── Document list theme chip ── */

.doc-theme-chip {
  border: none;
  border-radius: var(--radius-full);
  padding: 1px var(--space-2);
  font-size: 10px;
  font-weight: var(--font-medium);
  color: var(--color-neutral-0);
  cursor: pointer;
  max-width: 140px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  opacity: 0.85;
  transition: opacity var(--transition-fast), box-shadow var(--transition-fast);
}
.doc-theme-chip:hover {
  opacity: 1;
}
.doc-theme-chip.is-active {
  opacity: 1;
  box-shadow: var(--shadow-ring);
}
```

- [ ] **Step 5: Verify**

`cd frontend && npx eslint src/App.tsx` → 0 errors; `npm run build` → succeeds. Manual: with a clustered production, rows show colored chips matching the Brief's chip colors; clicking filters; clicking again clears. (Locally clusters may not exist — verify the no-chip path renders cleanly.)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/App.tsx frontend/src/styles/components.css
git commit -m "feat(frontend): theme chips in document list and grid"
```

---

### Task 7: Omnibox → chat ("Ask AI")

**Files:**
- Modify: `frontend/src/components/Omnibox.tsx`
- Modify: `frontend/src/components/AppHeader.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: Home's `chat.send` + `focusChat` (Task 5).
- Produces: `Omnibox` gains `onAsk?: (question: string) => void`; when the current mode is `'semantic'` AND `onAsk` is provided AND the query is non-blank, an extra "✦ Ask AI" button renders after the mode pill; clicking calls `onAsk(query.trim())` and clears the mode override (the query stays in the box). `AppHeader` gains `onAsk?: (question: string) => void`, forwarded to `<Omnibox onAsk={onAsk} …/>`. Home passes `onAsk={handleAsk}` where:

```tsx
  const handleAsk = useCallback((question: string) => {
    focusChat();
    chat.send(question);
  }, [chat, focusChat]);
```

(`chat` is stable per render but `send` closes over `attachedDocs` — including `chat` in deps is correct and lint-clean since the object identity changes with state; if eslint requires listing `chat.send` specifically, follow eslint.)

- [ ] **Step 1: Omnibox.** Add the prop and, in the button row next to `.omnibox-mode` (only when `mode === 'semantic' && onAsk && query.trim()`):

```tsx
        <button
          type="button"
          className="omnibox-tool omnibox-ask"
          onClick={() => { onAsk(query.trim()); setModeOverride(null); }}
          title="Send this question to the AI chat"
        >
          ✦ Ask AI
        </button>
```

CSS (append to layout.css near the omnibox block):

```css
.omnibox-ask {
  color: var(--color-brass);
  font-weight: var(--font-medium);
  white-space: nowrap;
}
.omnibox-ask:hover {
  color: var(--color-ink);
}
```

- [ ] **Step 2: AppHeader.** Add `onAsk?: (question: string) => void` to `AppHeaderProps`; forward: `{onSearch && <Omnibox onSearch={onSearch} initialQuery={initialQuery} onAsk={onAsk} />}`.

- [ ] **Step 3: App.tsx.** Add `handleAsk` (code above) and pass `onAsk={handleAsk}` in the `<AppHeader …/>` props.

- [ ] **Step 4: Verify**

`cd frontend && npx eslint src/components/Omnibox.tsx src/components/AppHeader.tsx src/App.tsx` → 0 errors; `npm run build` → succeeds. Manual: type a question in the omnibox → "✦ Ask AI" appears; clicking expands the rail, focuses nothing away from results (search is NOT run), and the question streams in the chat.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/Omnibox.tsx frontend/src/components/AppHeader.tsx frontend/src/App.tsx frontend/src/styles/layout.css
git commit -m "feat(frontend): Ask AI path from the omnibox into the rail chat"
```

---

### Task 8: Phase verification sweep

**Files:** none planned — fixes only.

- [ ] **Step 1: Builds/tests/lint**

`cd backend && python -m pytest tests/ -v` → Task 1's 2 new tests pass; only the known pre-existing failure remains.
`cd frontend && npm run build` → succeeds.
`cd frontend && npx eslint src/App.tsx src/hooks/useChat.ts src/components/ChatPanel.tsx src/components/ContextRail.tsx src/components/Omnibox.tsx src/components/AppHeader.tsx src/types/index.ts` → 0 errors.
`grep -rn "eslint-disable" frontend/src/hooks/useChat.ts frontend/src/components/ChatPanel.tsx frontend/src/components/ContextRail.tsx` → no matches.

- [ ] **Step 2: Live pass** (backend :8000 + Vite :5173, signed in, seeded production):
1. Rail renders expanded by default on Home; collapse → slim ✦ tab; reload → collapse state remembered.
2. Production state: context line renders (brief overview sentence or production name); "Ask the production…" → type a question → streams; Stop mid-stream keeps partial text; Copy/Download/Clear work.
3. Select one doc → rail flips to document state: bates/title header; Summarize (no local key → error toast, button re-enabled); Find similar (error toast locally); Open document opens the viewer; "Ask about this document" attaches a chip + focuses composer.
4. Select several docs → "Ask about these N" attaches all, chips removable.
5. Bulk bar shows Download/Tag/Clear only; floating bar sits centered over the list (shifted left of the rail).
6. No FAB, no overlay; `grep` checks from Task 5 clean.
7. Theme column: with no local clusters, no chips and no layout break; (with keys/clusters: chips colored consistently with the Brief, click-to-filter works).
8. Omnibox: question → "✦ Ask AI" → rail expands, question streams (search results untouched).
9. Production switch via header: chat resets (new production, fresh conversation) — expected per remount semantics.

- [ ] **Step 3: Commit fixes if any**

```bash
git add -A && git commit -m "fix: phase 3 verification fixes"
```

---

## Self-Review Notes

- **Spec §3-rail coverage:** three states ✔ (T4), ~380px collapsible ✔ (T4 CSS + T5 lift), chat re-homed reusing streaming/attachment UI ✔ (T2 extraction + T3), FAB deleted ✔ (T5), "Send to AI Agent" moved ✔ (T5 trim + multi state), bulk bar keeps tag/export ✔. §1 omnibox→chat ✔ (T7). List AI columns: theme chip ✔ (T6); relevance markers deliberately Phase 4. "Recent AI activity" in the idle rail is simplified to the brief context line — the pipeline's live activity feed adds polling complexity the spec doesn't require; noted as a Phase 5 nicety.
- **Sub-1024px:** rail hides entirely (documented limitation in the CSS comment); drawer variant deferred to Phase 5. This is a deliberate YAGNI cut — the firm reviews on desktop.
- **Type consistency check:** `ChatState` field names match between T2 definition and T3/T4/T5/T7 consumers; `ContextRailProps` matches the T5 call site including the T5-added `onAttached`; `autoFocusToken` threads T3←T4←T5; theme index mapping `(i % 8) + 1` identical in T6 and ProductionBrief.
- **Chat-reset-on-switch** is called out in constraints and the sweep (step 9) so nobody "fixes" it as a bug.
