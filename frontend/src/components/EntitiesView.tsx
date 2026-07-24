import { useCallback, useEffect, useState } from 'react';
import { acceptMergeSuggestion, autoResolveTypos, getEntityMentions, listEntities, listMergeSuggestions, mergeEntities, rejectMergeSuggestion, triggerEntityExtraction } from '../api/client';
import { entityDisplayName } from '../utils/entityDisplay';
import type { EntityListItem, MergeSuggestion } from '../types';
import EntityPanel from './EntityPanel';

interface CtxRow {
  bates: string;
  text: string;
  surface: string;
}

/** Snippet with the entity's surface text marker-highlighted. */
function CtxSnippet({ row }: { row: CtxRow }) {
  const idx = row.surface ? row.text.toLowerCase().indexOf(row.surface.toLowerCase()) : -1;
  return (
    <div className="merge-ctx-row">
      <span className="merge-ctx-bates">{row.bates}</span>
      {idx === -1 ? <>…{row.text}…</> : (
        <>
          …{row.text.slice(0, idx)}
          <span className="marker-hl">{row.text.slice(idx, idx + row.surface.length)}</span>
          {row.text.slice(idx + row.surface.length)}…
        </>
      )}
    </div>
  );
}

interface Props {
  productionId: number;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
  openEntityId?: string | null;
  onOpenEntityChange?: (id: string | null) => void;
}

// The keeper (merge winner) for a suggestion, defaulting to the more-frequent
// entity; an explicit choice in `winners` overrides the default.
function defaultKeeperId(s: MergeSuggestion): string {
  return s.entity_a.mention_count >= s.entity_b.mention_count ? s.entity_a.id : s.entity_b.id;
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function EntitiesView({ productionId, onViewDocument, onBack, openEntityId, onOpenEntityChange }: Props) {
  const [entities, setEntities] = useState<EntityListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [suggestions, setSuggestions] = useState<MergeSuggestion[]>([]);
  const openEntity = (id: string | null) => {
    onOpenEntityChange?.(id);
  };
  const [busy, setBusy] = useState<number | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [extracting, setExtracting] = useState(false);
  const [extractMsg, setExtractMsg] = useState<string | null>(null);
  // Bulk-select / winner-choice state for the suggestion queue.
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [winners, setWinners] = useState<Record<number, string>>({});
  const [bulkBusy, setBulkBusy] = useState(false);
  const [typoMsg, setTypoMsg] = useState<string | null>(null);

  // Hover context for merge review: "Isabella" vs "Isabel" is undecidable
  // without seeing each name in its documents. Snippets fetch lazily on
  // first hover and cache per entity.
  const [ctxCache, setCtxCache] = useState<Record<string, CtxRow[] | 'loading'>>({});
  const [hoverCtxId, setHoverCtxId] = useState<string | null>(null);

  const loadContext = (id: string) => {
    setCtxCache(prev => {
      if (prev[id]) return prev;
      getEntityMentions(id)
        .then(m => {
          const rows: CtxRow[] = [];
          for (const d of m.documents) {
            for (const mm of d.mentions) {
              if (rows.length >= 3) break;
              rows.push({ bates: d.bates_begin, text: mm.context_snippet || mm.surface_text, surface: mm.surface_text });
            }
            if (rows.length >= 3) break;
          }
          setCtxCache(p => ({ ...p, [id]: rows }));
        })
        .catch(() => setCtxCache(p => ({ ...p, [id]: [] })));
      return { ...prev, [id]: 'loading' };
    });
  };

  const mergeName = (e: EntityListItem, isKeeper?: boolean) => (
    <span
      className="merge-name-wrap"
      onMouseEnter={() => { loadContext(e.id); setHoverCtxId(e.id); }}
      onMouseLeave={() => setHoverCtxId(prev => (prev === e.id ? null : prev))}
    >
      <button
        className="btn btn-ghost btn-xs"
        style={{ fontWeight: 600, textDecoration: isKeeper ? 'underline' : undefined }}
        onClick={() => openEntity(e.id)}
      >
        {entityDisplayName(e.canonical_name, e.entity_type)}
      </button>
      <span className="merge-count">{e.mention_count}×</span>
      {hoverCtxId === e.id && (
        <div className="merge-ctx-pop">
          {(!ctxCache[e.id] || ctxCache[e.id] === 'loading') && <span className="def-meta">Pulling context…</span>}
          {Array.isArray(ctxCache[e.id]) && (ctxCache[e.id] as CtxRow[]).length === 0 && (
            <span className="def-meta">No mention snippets on file.</span>
          )}
          {Array.isArray(ctxCache[e.id]) && (ctxCache[e.id] as CtxRow[]).map((r, i) => <CtxSnippet key={i} row={r} />)}
        </div>
      )}
    </span>
  );

  const refresh = useCallback(() => {
    listEntities(productionId, search || undefined, typeFilter || undefined)
      .then(r => { setEntities(r.entities); setTotal(r.total); })
      .catch(e => console.warn('listEntities failed:', e));
    listMergeSuggestions(productionId)
      .then(setSuggestions)
      .catch(e => console.warn('listMergeSuggestions failed:', e));
  }, [productionId, search, typeFilter]);

  useEffect(() => { refresh(); }, [refresh]);

  // While a backfill runs, poll so names appear as documents are processed.
  useEffect(() => {
    if (!extracting) return;
    const timer = setInterval(refresh, 15000);
    return () => clearInterval(timer);
  }, [extracting, refresh]);

  const startExtraction = async (rebuild = false) => {
    if (rebuild && !window.confirm(
      'Rebuild the entity graph? All entities, relationships, events, and merge history for this matter will be deleted and re-extracted from scratch.',
    )) return;
    setExtractMsg(null);
    try {
      await triggerEntityExtraction(productionId, rebuild);
      setExtracting(true);
      setExtractMsg(rebuild
        ? 'Rebuild started — the old ontology is cleared; entities reappear below as documents are re-read.'
        : 'Extraction started — entities appear below as documents are processed.');
    } catch (e) {
      setExtractMsg(errText(e));
    }
  };

  const resolve = async (id: number, accept: boolean) => {
    setBusy(id);
    setResolveError(null);
    try {
      if (accept) await acceptMergeSuggestion(id);
      else await rejectMergeSuggestion(id);
      refresh();
    } catch (e) {
      setResolveError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  const keeperId = (s: MergeSuggestion): string => winners[s.id] ?? defaultKeeperId(s);
  const otherId = (s: MergeSuggestion): string => {
    const keeper = keeperId(s);
    return s.entity_a.id === keeper ? s.entity_b.id : s.entity_a.id;
  };

  // Single-row "Same — merge": routes through the same mergeEntities(winner,
  // loser) path bulk merge uses, so the keeper radio choice is honored —
  // acceptMergeSuggestion instead picks the winner by mention count and
  // silently discards it.
  const mergeSuggestion = async (s: MergeSuggestion) => {
    setBusy(s.id);
    setResolveError(null);
    try {
      await mergeEntities(keeperId(s), otherId(s));
      refresh();
    } catch (e) {
      setResolveError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  const setKeeper = (id: number, entityId: string) => {
    setWinners(prev => ({ ...prev, [id]: entityId }));
  };

  const toggleRow = (id: number) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allSelected = suggestions.length > 0 && suggestions.every(s => selected.has(s.id));
  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(suggestions.map(s => s.id)));
  };

  const selectedRows = suggestions.filter(s => selected.has(s.id));
  const selectedCount = selectedRows.length;

  const mergeSelected = async () => {
    if (selectedRows.length === 0) return;
    setBulkBusy(true);
    setResolveError(null);
    try {
      // Sequential, not parallel: overlapping pairs (A~B and B~C sharing B)
      // can both be selected via select-all. Running them concurrently lets
      // separate transactions interleave and produce two EntityMerge rows
      // that snapshot the same loser, corrupting undo. One at a time keeps
      // each merge's re-point + snapshot atomic relative to the next.
      const failures: string[] = [];
      for (const s of selectedRows) {
        try {
          await mergeEntities(keeperId(s), otherId(s));
        } catch (e) {
          failures.push(errText(e));
        }
      }
      if (failures.length > 0) {
        setResolveError(`${failures.length} of ${selectedRows.length} merges failed: ${failures.join('; ')}`);
      }
      setSelected(new Set());
      refresh();
    } finally {
      setBulkBusy(false);
    }
  };

  const dismissSelected = async () => {
    if (selectedRows.length === 0) return;
    setBulkBusy(true);
    setResolveError(null);
    try {
      const results = await Promise.allSettled(selectedRows.map(s => rejectMergeSuggestion(s.id)));
      const failures = results.filter(r => r.status === 'rejected') as PromiseRejectedResult[];
      if (failures.length > 0) {
        setResolveError(`${failures.length} of ${selectedRows.length} dismissals failed: ${failures.map(f => errText(f.reason)).join('; ')}`);
      }
      setSelected(new Set());
      refresh();
    } finally {
      setBulkBusy(false);
    }
  };

  const runAutoTypos = async () => {
    setBulkBusy(true);
    setResolveError(null);
    setTypoMsg(null);
    try {
      const { merged } = await autoResolveTypos(productionId);
      setTypoMsg(`Merged ${merged} obvious typo${merged === 1 ? '' : 's'}`);
      setSelected(new Set());
      refresh();
    } catch (e) {
      setResolveError(errText(e));
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <div style={{ position: 'relative', height: '100dvh', display: 'flex', flexDirection: 'column' }}>
      <div className="panel-header" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn btn-ghost btn-xs" onClick={onBack}>← Back</button>
        <span style={{ fontWeight: 600 }}>People &amp; Organizations ({total})</span>
        <span className="bates-chip">CAST&nbsp;OF&nbsp;CHARACTERS</span>
        <input
          className="input"
          placeholder="Search entities…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          style={{ marginLeft: 'auto', maxWidth: 240 }}
        />
        <select className="input" value={typeFilter} onChange={e => setTypeFilter(e.target.value)} style={{ maxWidth: 140 }}>
          <option value="">All types</option>
          <option value="person">People</option>
          <option value="org">Organizations</option>
        </select>
        <button
          className="btn btn-xs"
          disabled={extracting}
          onClick={() => startExtraction(false)}
          title="Run AI entity extraction over this matter's documents (manager only)"
        >
          {extracting ? 'Extracting…' : 'Extract entities'}
        </button>
        <button
          className="btn btn-ghost btn-xs"
          disabled={extracting}
          onClick={() => startExtraction(true)}
          title="Delete this matter's entire ontology and re-extract from scratch (manager only)"
        >
          Rebuild
        </button>
      </div>
      {extractMsg && (
        <div style={{ padding: '4px var(--space-4)', fontSize: 'var(--text-xs)', opacity: 0.8 }}>
          {extractMsg}
        </div>
      )}

      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)' }}>
        {suggestions.length > 0 && (
          <div className="card merge-queue" style={{ marginBottom: 16, padding: 'var(--space-4)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', marginBottom: 'var(--space-1)' }}>
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }} title="Select all suggestions">
                <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                <span className="bates-chip">MERGE&nbsp;REVIEW&nbsp;·&nbsp;{suggestions.length}</span>
              </label>
              <span className="def-meta">The AI thinks these may be the same. Suggestions merge only when you say so.</span>
              <button
                className="btn btn-ghost btn-xs"
                style={{ marginLeft: 'auto' }}
                disabled={bulkBusy}
                onClick={runAutoTypos}
                title="Auto-merge pairs that differ by a single-character typo (safe class only)"
              >
                Auto-merge obvious typos
              </button>
            </div>
            {typoMsg && (
              <div className="def-meta" style={{ marginTop: 'var(--space-2)' }}>
                {typoMsg}
              </div>
            )}
            {resolveError && (
              <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-danger-700)' }}>
                {resolveError}
              </div>
            )}
            {/* Sticky bulk action bar */}
            <div style={{
              position: 'sticky', top: 0, zIndex: 1,
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '6px 0', marginTop: 'var(--space-2)',
              background: 'var(--color-card)',
              borderBottom: '1px solid var(--color-neutral-100)',
            }}>
              <button
                className="btn btn-xs"
                disabled={bulkBusy || selectedCount === 0}
                onClick={mergeSelected}
              >
                Merge selected ({selectedCount})
              </button>
              <button
                className="btn btn-ghost btn-xs"
                disabled={bulkBusy || selectedCount === 0}
                onClick={dismissSelected}
              >
                Dismiss selected ({selectedCount})
              </button>
            </div>
            {suggestions.map(s => {
              const keeper = keeperId(s);
              const rowBusy = busy === s.id || bulkBusy;
              return (
                <div key={s.id} className="merge-row">
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                    <input
                      type="checkbox"
                      checked={selected.has(s.id)}
                      onChange={() => toggleRow(s.id)}
                      title="Select for bulk action"
                    />
                    <label style={{ display: 'inline-flex', alignItems: 'center', cursor: 'pointer' }} title="Keep this spelling as canonical">
                      <input
                        type="radio"
                        name={`keeper-${s.id}`}
                        checked={keeper === s.entity_a.id}
                        onChange={() => setKeeper(s.id, s.entity_a.id)}
                      />
                    </label>
                    {mergeName(s.entity_a, keeper === s.entity_a.id)}
                    <span className="merge-vs">↔</span>
                    <label style={{ display: 'inline-flex', alignItems: 'center', cursor: 'pointer' }} title="Keep this spelling as canonical">
                      <input
                        type="radio"
                        name={`keeper-${s.id}`}
                        checked={keeper === s.entity_b.id}
                        onChange={() => setKeeper(s.id, s.entity_b.id)}
                      />
                    </label>
                    {mergeName(s.entity_b, keeper === s.entity_b.id)}
                    <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 6 }}>
                      <button className="btn btn-secondary btn-xs" disabled={rowBusy} onClick={() => mergeSuggestion(s)}>Same — merge</button>
                      <button className="btn btn-ghost btn-xs" disabled={rowBusy} onClick={() => resolve(s.id, false)}>Different</button>
                    </span>
                  </div>
                  <div className="merge-rationale">"{s.rationale}"</div>
                </div>
              );
            })}
          </div>
        )}

        <table className="doc-table" style={{ width: '100%' }}>
          <thead>
            <tr><th>Name</th><th>Type</th><th>Mentions</th><th>Documents</th></tr>
          </thead>
          <tbody>
            {entities.map(e => (
              <tr key={e.id} style={{ cursor: 'pointer' }} onClick={() => openEntity(e.id)}>
                <td style={{ fontWeight: 600 }}>{entityDisplayName(e.canonical_name, e.entity_type)}</td>
                <td><span className={`entity-dot entity-${e.entity_type}`} style={{ marginRight: 5 }}>●</span>{e.entity_type === 'person' ? 'Person' : 'Org'}</td>
                <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--text-xs)' }}>{e.mention_count.toLocaleString()}</td>
                <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--text-xs)' }}>{e.document_count.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {entities.length === 0 && (
          <div className="empty-state">
            <div style={{ fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)', fontWeight: 700 }}>No cast of characters yet.</div>
            <div style={{ maxWidth: '46ch' }}>The AI reads the corpus and builds it: every person and organization, resolved across aliases, every relationship cited to its document.</div>
            <button className="btn btn-primary btn-sm" style={{ marginTop: 8 }} disabled={extracting} onClick={() => startExtraction(false)}>
              {extracting ? 'Extracting…' : 'Extract entities'}
            </button>
          </div>
        )}
      </div>

      {openEntityId && (
        <EntityPanel
          entityId={openEntityId}
          onClose={() => openEntity(null)}
          onOpenEntity={openEntity}
          onOpenDocument={docId => { openEntity(null); onViewDocument(docId); }}
        />
      )}
    </div>
  );
}
