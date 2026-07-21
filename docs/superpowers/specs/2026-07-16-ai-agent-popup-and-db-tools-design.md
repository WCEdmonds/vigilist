# AI Agent: non-blocking popup + database tools

**Date:** 2026-07-16
**Status:** Approved design, pending implementation plan
**Branch:** `feat/ai-agent-popup-db-tools`

## Summary

The AI Agent chat (shipped in `7ca58ee`, PR #15) is currently a **modal overlay**: a
full-screen dim backdrop (`.ai-agent-overlay`) blocks the page and closes the panel on
outside-click, and the backend `/api/ai/chat` endpoint is a single streaming completion
with **no tools** — document context is pre-stuffed into the system prompt.

This work makes two changes:

1. **Non-blocking popup** — convert the modal into a corner-docked, resizable panel so the
   user can navigate the site (search, open documents, page the viewer) while chatting.
2. **Database tools** — give the agent read-only tool-use so it can query the corpus itself
   instead of relying only on pre-attached document text.

All tools are **read-only** and filtered to the authenticated user's accessible productions.

## Current state (as of `origin/main`)

- **`frontend/src/components/AIAgent.tsx`** — renders `.ai-agent-overlay` (fixed, `inset:0`,
  `z-index:250`, `onClick={onClose}`) wrapping a centered `.ai-agent-panel` (760×82vh).
  Handles streaming, transcript copy/download, attached-doc chips, Escape-to-close. Kept
  mounted so the session conversation persists across open/close.
- **`frontend/src/App.tsx`** — owns `chatOpen` / `chatDocs` state, the `.ai-agent-fab`
  launcher (hidden while open), and `sendSelectionToAgent()` ("Send to AI Agent" bulk action
  that attaches selected docs by id + Bates label).
- **`frontend/src/api/client.ts`** — `streamChat(messages, docIds, {onDelta, onError}, signal)`
  parses SSE frames: `{type:"delta"|"done"|"error"}`.
- **`backend/app/routers/ai.py`** — `POST /api/ai/chat`: sanitizes history (`_MAX_CHAT_MESSAGES`),
  resolves `doc_ids` with access control (`_MAX_CHAT_DOCS`, `get_accessible_production_ids`),
  builds a system prompt with embedded doc text, streams from Claude via `messages.stream`.
- **`backend/app/services/ai.py`** — `CHAT_MODEL = "claude-opus-4-8"`, `CHAT_SYSTEM_PROMPT`,
  `build_chat_system_prompt(documents)` (embeds up to `_CHAT_DOC_CHAR_LIMIT` chars/doc).
- **`frontend/src/styles/components.css`** — `.ai-agent-fab`, `.ai-agent-overlay`,
  `.ai-agent-panel`, header/docs/body/composer styles, and a `<768px` full-screen fallback.

## Part 1 — Non-blocking popup

### Behavior
- Remove the `.ai-agent-overlay` wrapper entirely. The panel renders directly as a
  `position: fixed` element anchored bottom-right (where the FAB sits). No dim layer → the
  site behind stays fully interactive (clickable + scrollable).
- **Resizable** via a drag handle on the panel's **top-left corner** (panel grows up-and-left
  from its bottom-right anchor). Size clamped to viewport bounds; persisted to `localStorage`
  under `vigilist.aiAgent.size`. Default ≈ 420×620.
- **Close:** header ✕ button and the FAB (reappears on close, unchanged). Remove
  outside-click-close (no backdrop). Keep **Escape-to-close only when focus is within the
  panel** — a global Escape listener would dismiss the chat while the user works elsewhere.
- **Mobile (<768px):** keep the existing full-screen fallback (a floating corner panel is not
  useful on a phone). The resize handle is hidden at this breakpoint.
- Unchanged: streaming, transcript copy/download, attached-doc chips, staying mounted for
  session persistence.

### Files touched
- `frontend/src/components/AIAgent.tsx` — drop overlay wrapper; add resize handle + size
  state/persistence; scope Escape to panel focus.
- `frontend/src/styles/components.css` — replace `.ai-agent-overlay`/`.ai-agent-panel` modal
  styling with docked-panel + resize-handle styling; keep the mobile fallback.
- `frontend/src/App.tsx` — no logic change expected (FAB + mount already correct); verify
  z-index layering against existing modals/toasts.

## Part 2 — Database tools (backend agentic loop)

`/api/ai/chat` changes from a single streaming completion to a **bounded tool-use loop**:

```
build system prompt + tool definitions
loop (max MAX_TOOL_ROUNDS = 8):
    stream one assistant turn
      - text deltas       -> SSE {type:"delta"}
      - tool_use blocks   -> emit {type:"tool_use"}, execute tool, emit {type:"tool_result"}
    if the turn used no tools: break
    else append assistant turn + tool_result blocks to messages and continue
emit {type:"done"}
```

Implemented with the Anthropic SDK streaming + tool-use (accumulate `tool_use` blocks per
turn, run them, feed `tool_result` blocks back as the next user turn).

### Tools (all read-only; every tool re-derives access from the authenticated user)

| Tool | Input | Wraps | Returns |
|------|-------|-------|---------|
| `search_documents` | `query`, `production_id?`, `file_type?`, `page?` | `services.search.search_documents` (with `accessible_production_ids`) | list of `{id, bates, title, snippet}`, total |
| `get_document` | `bates_or_id` | `db.get(Document)` + access check | full text (capped) + metadata |
| `list_productions` | — | accessible productions | `[{id, name, doc_count}]` |
| `find_similar_documents` | `bates_or_id` | `services.semantic_search` / find-similar path | list of `{id, bates, title, similarity}` |
| `get_duplicates` | `bates_or_id` | per-doc duplicates lookup (as in `routers/intelligence.py`) | list of `{document_id, bates, similarity, type}` |
| `get_corpus_stats` | `production_id` | dashboard service | doc/page counts + tag breakdown |

### Safety & bounds
- **Access control:** every tool computes `get_accessible_production_ids(user)` and filters to
  it. A document or production outside the user's scope is unreachable and returns a
  "not found / no access" tool result — never leaks existence or content.
- **Read-only:** no writes, no audit-log entries generated by tools.
- **Token bounds:** per-tool result caps (e.g. document text truncated like
  `_CHAT_DOC_CHAR_LIMIT`; search results capped to a page). `MAX_TOOL_ROUNDS = 8` bounds the
  loop.
- **Model:** unchanged `claude-opus-4-8`.

### Attached documents
The "Send to AI Agent" flow is unchanged — attached docs remain pinned context in the system
prompt via `build_chat_system_prompt`. The `get_document` tool is additive: the agent can pull
*other* documents on demand. (No change to `_MAX_CHAT_DOCS` behavior.)

### Files touched
- `backend/app/routers/ai.py` — replace the single-stream body of `chat()` with the tool loop;
  emit the new SSE event types.
- **`backend/app/services/ai_tools.py`** (new) — tool JSON-schema definitions, a `dispatch`
  helper mapping tool name → implementation, and the read-only tool implementations
  (each taking `db`, `user`, and validated input). Keeps `ai.py`/`routers/ai.py` focused on
  prompt-building and the stream loop respectively.

## Part 3 — Streaming protocol (the seam)

Extend SSE event types:
- Existing: `{type:"delta", text}`, `{type:"done"}`, `{type:"error", message}`.
- New: `{type:"tool_use", name, summary}` — human-readable summary, e.g.
  *"Searching documents for 'termination clause'"*.
- New: `{type:"tool_result", name, ok, summary?}` — e.g. *"12 documents found"*.

### Files touched
- `frontend/src/api/client.ts` — `streamChat` handlers gain `onToolUse` / `onToolResult`;
  parse the new frame types.
- `frontend/src/components/AIAgent.tsx` — render tool activity as small inline status rows in
  the transcript (e.g. `🔍 Searched documents · 12 hits`) interleaved with assistant text, so
  the agent's DB work is visible rather than hidden.

## Testing

**Backend** (extend `backend/tests/`, following `test_org_access.py` / `test_search_query.py`):
- Each tool wrapper enforces access scope — a document/production outside the user's accessible
  set is unreachable.
- The tool loop terminates at `MAX_TOOL_ROUNDS`.
- SSE frames for a tool round are well-formed (`tool_use` → `tool_result` → `delta` → `done`).
- Tool result token caps applied.

**Frontend** (manual):
- Site remains interactive (click/scroll/navigate, open a document) with the panel open.
- Resize works and size persists across reload.
- Tool-status rows render during a query.
- Escape closes only when focus is in the panel; mobile falls back to full-screen.

## Out of scope (v1)
- Write actions (apply/remove tags, add notes).
- Draggable free-positioning of the panel (only corner-docked + resizable).
- Cross-session conversation persistence (still session-only, in-memory).
- Reconciling the parallel `origin/claude/ai-chatbot-implementation-hb255g` branch — left
  untouched; reconcile separately if desired.
