import { useCallback, useEffect, useState } from 'react';
import { acceptMergeSuggestion, listEntities, listMergeSuggestions, rejectMergeSuggestion, triggerEntityExtraction } from '../api/client';
import { entityDisplayName } from '../utils/entityDisplay';
import type { EntityListItem, MergeSuggestion } from '../types';
import EntityPanel from './EntityPanel';

interface Props {
  productionId: number;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
  openEntityId?: string | null;
  onOpenEntityChange?: (id: string | null) => void;
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
              <span className="bates-chip">MERGE&nbsp;REVIEW&nbsp;·&nbsp;{suggestions.length}</span>
              <span className="def-meta">The AI thinks these may be the same. Nothing merges without you.</span>
            </div>
            {resolveError && (
              <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-danger-700)' }}>
                {resolveError}
              </div>
            )}
            {suggestions.map(s => (
              <div key={s.id} className="merge-row">
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  <button className="btn btn-ghost btn-xs" style={{ fontWeight: 600 }} onClick={() => openEntity(s.entity_a.id)}>
                    {entityDisplayName(s.entity_a.canonical_name)}
                  </button>
                  <span className="merge-count">{s.entity_a.mention_count}×</span>
                  <span className="merge-vs">↔</span>
                  <button className="btn btn-ghost btn-xs" style={{ fontWeight: 600 }} onClick={() => openEntity(s.entity_b.id)}>
                    {entityDisplayName(s.entity_b.canonical_name)}
                  </button>
                  <span className="merge-count">{s.entity_b.mention_count}×</span>
                  <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 6 }}>
                    <button className="btn btn-secondary btn-xs" disabled={busy === s.id} onClick={() => resolve(s.id, true)}>Same — merge</button>
                    <button className="btn btn-ghost btn-xs" disabled={busy === s.id} onClick={() => resolve(s.id, false)}>Different</button>
                  </span>
                </div>
                <div className="merge-rationale">"{s.rationale}"</div>
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
                <td style={{ fontWeight: 600 }}>{entityDisplayName(e.canonical_name)}</td>
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
