import { useCallback, useEffect, useState } from 'react';
import { acceptMergeSuggestion, listEntities, listMergeSuggestions, rejectMergeSuggestion } from '../api/client';
import type { EntityListItem, MergeSuggestion } from '../types';
import EntityPanel from './EntityPanel';

interface Props {
  productionId: number;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
}

export default function EntitiesView({ productionId, onViewDocument, onBack }: Props) {
  const [entities, setEntities] = useState<EntityListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [suggestions, setSuggestions] = useState<MergeSuggestion[]>([]);
  const [openEntityId, setOpenEntityId] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const refresh = useCallback(() => {
    listEntities(productionId, search || undefined, typeFilter || undefined)
      .then(r => { setEntities(r.entities); setTotal(r.total); })
      .catch(e => console.warn('listEntities failed:', e));
    listMergeSuggestions(productionId)
      .then(setSuggestions)
      .catch(e => console.warn('listMergeSuggestions failed:', e));
  }, [productionId, search, typeFilter]);

  useEffect(() => { refresh(); }, [refresh]);

  const resolve = async (id: number, accept: boolean) => {
    setBusy(id);
    try {
      if (accept) await acceptMergeSuggestion(id);
      else await rejectMergeSuggestion(id);
      refresh();
    } catch (e) {
      console.warn('merge suggestion resolution failed:', e);
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
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)' }}>
        {suggestions.length > 0 && (
          <div className="card" style={{ marginBottom: 16, padding: 'var(--space-4)' }}>
            <div className="panel-header" style={{ padding: 0 }}>
              Possible duplicates — same person? ({suggestions.length})
            </div>
            {suggestions.map(s => (
              <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
                <span>
                  <b>{s.entity_a.canonical_name}</b> ({s.entity_a.mention_count})
                  {' ↔ '}
                  <b>{s.entity_b.canonical_name}</b> ({s.entity_b.mention_count})
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
              <tr key={e.id} style={{ cursor: 'pointer' }} onClick={() => setOpenEntityId(e.id)}>
                <td>{e.canonical_name}</td>
                <td>{e.entity_type === 'person' ? 'Person' : 'Org'}</td>
                <td>{e.mention_count}</td>
                <td>{e.document_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {entities.length === 0 && <div className="empty-state">No entities extracted yet.</div>}
      </div>

      {openEntityId && (
        <EntityPanel
          entityId={openEntityId}
          onClose={() => setOpenEntityId(null)}
          onOpenEntity={setOpenEntityId}
          onOpenDocument={docId => { setOpenEntityId(null); onViewDocument(docId); }}
        />
      )}
    </div>
  );
}
