import { useCallback, useEffect, useState } from 'react';
import { acceptMergeSuggestion, autoResolveTypos, listEntities, listMergeSuggestions, mergeEntities, rejectMergeSuggestion, triggerEntityExtraction } from '../api/client';
import type { EntityListItem, MergeSuggestion } from '../types';
import EntityPanel from './EntityPanel';

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

  const startExtraction = async () => {
    setExtractMsg(null);
    try {
      await triggerEntityExtraction(productionId);
      setExtracting(true);
      setExtractMsg('Extraction started — entities appear below as documents are processed.');
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
      const results = await Promise.allSettled(selectedRows.map(s => {
        const winnerId = keeperId(s);
        const loserId = s.entity_a.id === winnerId ? s.entity_b.id : s.entity_a.id;
        return mergeEntities(winnerId, loserId);
      }));
      const failures = results.filter(r => r.status === 'rejected') as PromiseRejectedResult[];
      if (failures.length > 0) {
        setResolveError(`${failures.length} of ${selectedRows.length} merges failed: ${failures.map(f => errText(f.reason)).join('; ')}`);
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
          onClick={startExtraction}
          title="Run AI entity extraction over this matter's documents (manager only)"
        >
          {extracting ? 'Extracting…' : 'Extract entities'}
        </button>
      </div>
      {extractMsg && (
        <div style={{ padding: '4px var(--space-4)', fontSize: 'var(--text-xs)', opacity: 0.8 }}>
          {extractMsg}
        </div>
      )}

      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)' }}>
        {suggestions.length > 0 && (
          <div className="card" style={{ marginBottom: 16, padding: 'var(--space-4)' }}>
            <div className="panel-header" style={{ padding: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }} title="Select all suggestions">
                <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                <span>Possible duplicates — same person? ({suggestions.length})</span>
              </label>
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
              <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', opacity: 0.8 }}>
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
              background: 'var(--color-surface, #fff)',
              borderBottom: '1px solid var(--color-border, #e5e7eb)',
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
                <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
                  <input
                    type="checkbox"
                    checked={selected.has(s.id)}
                    onChange={() => toggleRow(s.id)}
                    title="Select for bulk action"
                  />
                  <span style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
                    <label style={{ display: 'inline-flex', alignItems: 'center', cursor: 'pointer' }} title="Keep this spelling as canonical">
                      <input
                        type="radio"
                        name={`keeper-${s.id}`}
                        checked={keeper === s.entity_a.id}
                        onChange={() => setKeeper(s.id, s.entity_a.id)}
                      />
                    </label>
                    <button
                      className="btn btn-ghost btn-xs"
                      style={{ fontWeight: 600, textDecoration: keeper === s.entity_a.id ? 'underline' : 'none' }}
                      onClick={() => openEntity(s.entity_a.id)}
                    >
                      {s.entity_a.canonical_name}
                    </button>
                    <span style={{ opacity: 0.6 }}>({s.entity_a.mention_count})</span>
                    <span style={{ opacity: 0.6 }}>{' ↔ '}</span>
                    <label style={{ display: 'inline-flex', alignItems: 'center', cursor: 'pointer' }} title="Keep this spelling as canonical">
                      <input
                        type="radio"
                        name={`keeper-${s.id}`}
                        checked={keeper === s.entity_b.id}
                        onChange={() => setKeeper(s.id, s.entity_b.id)}
                      />
                    </label>
                    <button
                      className="btn btn-ghost btn-xs"
                      style={{ fontWeight: 600, textDecoration: keeper === s.entity_b.id ? 'underline' : 'none' }}
                      onClick={() => openEntity(s.entity_b.id)}
                    >
                      {s.entity_b.canonical_name}
                    </button>
                    <span style={{ opacity: 0.6 }}>({s.entity_b.mention_count})</span>
                  </span>
                  <span style={{ opacity: 0.6, fontSize: 'var(--text-xs)' }}>{s.rationale}</span>
                  <span style={{ marginLeft: 'auto' }}>
                    <button className="btn btn-xs" disabled={rowBusy} onClick={() => resolve(s.id, true)}>Same — merge</button>
                    <button className="btn btn-ghost btn-xs" disabled={rowBusy} onClick={() => resolve(s.id, false)}>Different</button>
                  </span>
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
                <td>{e.canonical_name}</td>
                <td>{e.entity_type === 'person' ? 'Person' : 'Org'}</td>
                <td>{e.mention_count}</td>
                <td>{e.document_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {entities.length === 0 && (
          <div className="empty-state">
            <div>No entities extracted yet.</div>
            <button className="btn btn-xs" style={{ marginTop: 8 }} disabled={extracting} onClick={startExtraction}>
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
