import { useCallback, useEffect, useRef, useState } from 'react';
import ChatPanel from './ChatPanel';
import { deleteConversation, findSimilar, getDocument, getPipeline, listConversations, summarizeDocument } from '../api/client';
import { showToast } from './Toast';
import type { ChatState } from '../hooks/useChat';
import type { AttachedDoc, ConversationSummary, DocumentSummary, ProductionInfo, SearchResult } from '../types';

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
  onAttached?: () => void;
  onOpenEntity?: (id: string) => void;
}

interface DocMeta {
  title: string | null;
  bates_begin: string;
  bates_end: string;
}

// Grabs the first sentence of a brief overview for the one-line context
// summary; falls back to the whole string if no sentence terminator is found.
function firstSentence(text: string): string {
  const match = text.match(/^[\s\S]*?[.!?](?=\s|$)/);
  return (match ? match[0] : text).trim();
}

export default function ContextRail({
  production,
  chat,
  collapsed,
  onToggleCollapsed,
  autoFocusToken,
  selectedIds,
  documents,
  searchResults,
  onViewDocument,
  onSimilarResults,
  onAttached,
  onOpenEntity,
}: ContextRailProps) {
  const [contextLine, setContextLine] = useState<string | null>(null);
  const [docSummaries, setDocSummaries] = useState<Record<string, string | null>>({});
  const [summarizing, setSummarizing] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  // Fetched when the panel opens rather than on mount — history is a
  // deliberate detour and most sessions never ask for it.
  const refreshHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      setConversations(await listConversations(production.id));
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Could not load chat history', 'error');
    } finally {
      setHistoryLoading(false);
    }
  }, [production.id]);

  const toggleHistory = useCallback(() => {
    const next = !historyOpen;
    setHistoryOpen(next);
    if (next) void refreshHistory();
  }, [historyOpen, refreshHistory]);

  const openConversation = useCallback(async (id: string) => {
    await chat.loadConversation(id);
    setHistoryOpen(false);
  }, [chat]);

  const removeConversation = useCallback(async (id: string) => {
    try {
      await deleteConversation(id);
      setConversations(prev => prev.filter(c => c.id !== id));
      // If the open thread is the one just deleted, detach: the next question
      // should start a new conversation, not write into a dead id.
      if (chat.conversationId === id) chat.clear();
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Could not delete conversation', 'error');
    }
  }, [chat]);
  const [findingSimilar, setFindingSimilar] = useState(false);
  const fetchedDocIds = useRef(new Set<string>());

  // One-line production context, fetched once per production (no polling).
  useEffect(() => {
    let cancelled = false;
    getPipeline(production.id)
      .then(info => {
        if (cancelled) return;
        const overview = info.brief?.overview;
        setContextLine(overview ? firstSentence(overview) : production.name);
      })
      .catch(err => {
        if (cancelled) return;
        console.warn('Failed to load pipeline info for context rail', err);
        setContextLine(production.name);
      });
    return () => { cancelled = true; };
  }, [production.id, production.name]);

  // Lazy per-document fetch: only runs when exactly one document is
  // selected, and only once per id (tracked via fetchedDocIds).
  const selectedKey = Array.from(selectedIds).sort().join(',');
  useEffect(() => {
    const ids = selectedKey ? selectedKey.split(',') : [];
    if (ids.length !== 1) return;
    const id = ids[0];
    const idCache = fetchedDocIds.current;
    if (idCache.has(id)) return;
    idCache.add(id);

    let cancelled = false;
    let settled = false;
    getDocument(id)
      .then(detail => {
        settled = true;
        if (cancelled) return;
        const summary = detail.summary && detail.summary.trim() ? detail.summary : null;
        setDocSummaries(prev => ({ ...prev, [id]: summary }));
      })
      .catch(err => {
        settled = true;
        if (cancelled) return;
        console.warn('Failed to load document for context rail', err);
        // Let the user retry via the "Summarize" button rather than being
        // stuck on a permanent loading state.
        setDocSummaries(prev => ({ ...prev, [id]: null }));
      });
    return () => {
      cancelled = true;
      // If we're torn down (selection changed) before the fetch settled,
      // un-mark the id so re-selecting it later retries instead of being
      // stuck on "Loading…" forever with a poisoned cache entry.
      if (!settled) idCache.delete(id);
    };
  }, [selectedKey]);

  // Label resolution mirrors the old sendSelectionToAgent: bates from
  // searchResults first, then documents, else a truncated id.
  const labelFor = useCallback((id: string): string => {
    const fromSearch = searchResults.find(r => r.id === id);
    if (fromSearch) return fromSearch.bates_begin;
    const fromDocs = documents.find(d => d.id === id);
    return fromDocs?.bates_begin ?? id.slice(0, 8);
  }, [searchResults, documents]);

  const metaFor = useCallback((id: string): DocMeta | null => {
    const fromSearch = searchResults.find(r => r.id === id);
    if (fromSearch) return fromSearch;
    const fromDocs = documents.find(d => d.id === id);
    if (fromDocs) return fromDocs;
    return null;
  }, [searchResults, documents]);

  const handleSummarize = async (id: string) => {
    setSummarizing(true);
    try {
      const res = await summarizeDocument(id);
      const summary = res.summary && res.summary.trim() ? res.summary : null;
      setDocSummaries(prev => ({ ...prev, [id]: summary }));
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Summarize failed', 'error');
    } finally {
      setSummarizing(false);
    }
  };

  const handleFindSimilar = async (id: string, label: string) => {
    setFindingSimilar(true);
    try {
      const res = await findSimilar(id);
      onSimilarResults(`Similar to ${label}`, res.results);
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Find similar failed', 'error');
    } finally {
      setFindingSimilar(false);
    }
  };

  const handleAskDoc = (id: string, label: string) => {
    chat.attachDocs([{ id, label }]);
    onAttached?.();
  };

  const handleAskMulti = () => {
    const docs: AttachedDoc[] = Array.from(selectedIds).map(id => ({ id, label: labelFor(id) }));
    chat.attachDocs(docs);
    onAttached?.();
  };

  if (collapsed) {
    return (
      <button
        type="button"
        className="context-rail-tab"
        onClick={onToggleCollapsed}
        aria-label="Expand intelligence rail"
        aria-expanded={false}
      >
        <span aria-hidden="true">✦</span>
        <span className="rail-tab-label">Intelligence</span>
      </button>
    );
  }

  const selectedIdList = Array.from(selectedIds);
  const selectedCount = selectedIdList.length;
  const singleId = selectedCount === 1 ? selectedIdList[0] : null;
  const singleLabel = singleId ? labelFor(singleId) : null;
  const singleMeta = singleId ? metaFor(singleId) : null;
  const singleTitle = singleMeta?.title || singleLabel;
  const singleSummary = singleId ? docSummaries[singleId] : undefined;

  let placeholder = 'Ask the production…';
  if (singleLabel) placeholder = `Ask about ${singleLabel}…`;
  else if (selectedCount > 1) placeholder = `Ask about ${selectedCount} documents…`;

  return (
    <aside className={`context-rail${!collapsed ? ' is-drawer-open' : ''}`}>
      <div className="rail-header">
        <span className="rail-title">✦ Intelligence</span>
        {/* Labelled, not a bare glyph: "⟲" gave no clue this opened saved
            conversations, and the rail has room for a word. */}
        <button
          type="button"
          className="rail-history-btn"
          onClick={toggleHistory}
          aria-expanded={historyOpen}
          title="Past AI conversations in this matter"
        >
          ⟲ History
        </button>
        <button
          type="button"
          className="btn-icon"
          onClick={onToggleCollapsed}
          aria-label="Collapse intelligence rail"
          aria-expanded={true}
        >
          ▸
        </button>
      </div>

      {historyOpen && (
        <div style={{
          borderBottom: '1px solid var(--color-neutral-200)',
          maxHeight: 280, overflowY: 'auto', padding: 'var(--space-2)',
        }}>
          {historyLoading && (
            <div style={{ padding: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>
              Loading history…
            </div>
          )}
          {!historyLoading && conversations.length === 0 && (
            <div style={{ padding: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>
              No saved conversations in this matter yet.
            </div>
          )}
          {conversations.map(c => (
            <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <button
                type="button"
                onClick={() => void openConversation(c.id)}
                style={{
                  flex: 1, minWidth: 0, textAlign: 'left', cursor: 'pointer',
                  background: c.id === chat.conversationId ? 'rgba(44,62,107,0.06)' : 'transparent',
                  border: 'none', font: 'inherit', color: 'inherit',
                  padding: 'var(--space-2)', borderRadius: 'var(--radius-sm)',
                }}
              >
                <span style={{
                  display: 'block', fontSize: 'var(--text-xs)', fontWeight: 600,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                  {c.title || 'Untitled conversation'}
                </span>
                <span style={{ display: 'block', fontSize: 11, color: 'var(--color-neutral-500)' }}>
                  {c.message_count} message{c.message_count === 1 ? '' : 's'}
                  {c.created_by_email ? ` · ${c.created_by_email}` : ''}
                </span>
              </button>
              <button
                type="button"
                onClick={() => void removeConversation(c.id)}
                aria-label={`Delete conversation ${c.title || ''}`}
                title="Delete conversation"
                style={{
                  background: 'none', border: 'none', cursor: 'pointer',
                  color: 'var(--color-neutral-400)', fontSize: 16, padding: '0 6px',
                }}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {selectedCount === 0 && contextLine && (
        <div className="rail-context-line">{contextLine}</div>
      )}

      {singleId && singleLabel && (
        <>
          <div className="rail-doc-header">
            <div className="rail-doc-title">{singleTitle}</div>
            {singleMeta && (
              <div className="rail-doc-bates">
                {singleMeta.bates_begin}
                {singleMeta.bates_begin !== singleMeta.bates_end && ` – ${singleMeta.bates_end}`}
              </div>
            )}
          </div>

          {singleSummary ? (
            <div className="rail-summary">{singleSummary}</div>
          ) : singleSummary === null ? (
            <div className="rail-actions">
              <button
                type="button"
                className="btn btn-secondary btn-xs"
                onClick={() => handleSummarize(singleId)}
                disabled={summarizing}
              >
                {summarizing ? 'Summarizing…' : '✦ Summarize'}
              </button>
            </div>
          ) : (
            <div className="rail-summary">Loading…</div>
          )}

          <div className="rail-actions">
            <button
              type="button"
              className="btn btn-secondary btn-xs"
              onClick={() => handleFindSimilar(singleId, singleLabel)}
              disabled={findingSimilar}
            >
              {findingSimilar ? 'Finding…' : 'Find similar'}
            </button>
            <button type="button" className="btn btn-secondary btn-xs" onClick={() => onViewDocument(singleId)}>
              Open document
            </button>
            <button type="button" className="btn btn-secondary btn-xs" onClick={() => handleAskDoc(singleId, singleLabel)}>
              ✦ Ask about this document
            </button>
          </div>
        </>
      )}

      {selectedCount > 1 && (
        <>
          <div className="rail-multi-line">{selectedCount} documents selected</div>
          <div className="rail-actions">
            <button type="button" className="btn btn-secondary btn-xs" onClick={handleAskMulti}>
              ✦ Ask about these {selectedCount}
            </button>
          </div>
        </>
      )}

      <ChatPanel chat={chat} placeholder={placeholder} autoFocusToken={autoFocusToken} onOpenDocument={onViewDocument} onOpenEntity={onOpenEntity} productionId={production.id} />
    </aside>
  );
}
