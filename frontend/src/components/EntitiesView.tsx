import { useCallback, useEffect, useState } from 'react';
import { acceptMergeSuggestion, listEntities, listMergeSuggestions, rejectMergeSuggestion, triggerEntityExtraction } from '../api/client';
import type { EntityListItem, MergeSuggestion } from '../types';
import EntityPanel from './EntityPanel';

interface Props {
  productionId: number;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
  initialEntityId?: string | null;
  onOpenEntityChange?: (id: string | null) => void;
}

export default function EntitiesView({ productionId, onViewDocument, onBack, initialEntityId, onOpenEntityChange }: Props) {
  const [entities, setEntities] = useState<EntityListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [suggestions, setSuggestions] = useState<MergeSuggestion[]>([]);
  const [openEntityId, setOpenEntityId] = useState<string | null>(initialEntityId ?? null);
  const openEntity = (id: string | null) => {
    setOpenEntityId(id);
    onOpenEntityChange?.(id);
  };
  const [busy, setBusy] = useState<number | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [extracting, setExtracting] = useState(false);
  const [extractMsg, setExtractMsg] = useState<string | null>(null);

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
      setExtractMsg(e instanceof Error ? e.message : String(e));
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
      setResolveError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div style={{ position: 'relative', height: '100%', display: 'flex', flexDirection: 'column' }}>
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
            <div className="panel-header" style={{ padding: 0 }}>
              Possible duplicates — same person? ({suggestions.length})
            </div>
            {resolveError && (
              <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-danger-700)' }}>
                {resolveError}
              </div>
            )}
            {suggestions.map(s => (
              <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
                <span>
                  <button className="btn btn-ghost btn-xs" style={{ fontWeight: 600 }} onClick={() => openEntity(s.entity_a.id)}>
                    {s.entity_a.canonical_name}
                  </button> ({s.entity_a.mention_count})
                  {' ↔ '}
                  <button className="btn btn-ghost btn-xs" style={{ fontWeight: 600 }} onClick={() => openEntity(s.entity_b.id)}>
                    {s.entity_b.canonical_name}
                  </button> ({s.entity_b.mention_count})
                </span>
                <span style={{ opacity: 0.6, fontSize: 'var(--text-xs)' }}>{s.rationale}</span>
                <span style={{ marginLeft: 'auto' }}>
                  <button className="btn btn-xs" disabled={busy === s.id} onClick={() => resolve(s.id, true)}>Same — merge</button>
                  <button className="btn btn-ghost btn-xs" disabled={busy === s.id} onClick={() => resolve(s.id, false)}>Different</button>
                </span>
              </div>
            ))}
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
