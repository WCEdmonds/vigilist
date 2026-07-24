import { useCallback, useEffect, useState } from 'react';
import { getTimeline, listEntities } from '../api/client';
import type { EntityListItem, TimelineEvent } from '../types';
import EntityPanel from './EntityPanel';

interface Props {
  productionId: number;
  openEntityId?: string | null;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
  onOpenEntityChange?: (id: string | null) => void;
}

const TYPE_BADGES: Record<string, string> = {
  meeting: 'Meeting', communication: 'Communication', payment: 'Payment',
  filing: 'Filing', agreement: 'Agreement', other: 'Event',
};

function dateLabel(e: TimelineEvent): string {
  if (!e.event_date) return 'Undated';
  const d = new Date(e.event_date + 'T00:00:00');
  if (e.date_precision === 'year') return String(d.getFullYear());
  if (e.date_precision === 'month') return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function groupKey(e: TimelineEvent): string {
  if (!e.event_date) return 'Undated';
  const d = new Date(e.event_date + 'T00:00:00');
  if (e.date_precision === 'year') return String(d.getFullYear());
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
}

export default function EntityTimelineView({ productionId, openEntityId, onViewDocument, onBack, onOpenEntityChange }: Props) {
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [undatedCount, setUndatedCount] = useState(0);
  const [page, setPage] = useState(1);
  const [entityFilter, setEntityFilter] = useState<string>('');
  const [typeFilter, setTypeFilter] = useState('');
  const [filterOptions, setFilterOptions] = useState<EntityListItem[]>([]);
  const [showUndated, setShowUndated] = useState(false);

  const openEntity = (id: string | null) => { onOpenEntityChange?.(id); };

  useEffect(() => {
    listEntities(productionId, undefined, undefined, 1, 100)
      .then(r => setFilterOptions(r.entities))
      .catch(e => console.warn('listEntities failed:', e));
  }, [productionId]);

  const load = useCallback((pageNum: number, append: boolean) => {
    getTimeline(productionId, entityFilter || undefined, typeFilter || undefined, pageNum)
      .then(r => {
        setEvents(prev => (append ? [...prev, ...r.events] : r.events));
        setTotal(r.total);
        setUndatedCount(r.undated_count);
        setPage(pageNum);
      })
      .catch(e => console.warn('getTimeline failed:', e));
  }, [productionId, entityFilter, typeFilter]);

  useEffect(() => { load(1, false); }, [load]);

  const dated = events.filter(e => e.event_date);
  const undated = events.filter(e => !e.event_date);
  const groups: { key: string; items: TimelineEvent[] }[] = [];
  for (const e of dated) {
    const key = groupKey(e);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(e);
    else groups.push({ key, items: [e] });
  }

  const renderEvent = (e: TimelineEvent) => (
    <div key={e.event_id} className="card" style={{ padding: 'var(--space-3)', marginBottom: 8 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <span className="stamp-badge stamp-badge--ink">{TYPE_BADGES[e.event_type] || e.event_type}</span>
        <span style={{ fontSize: 'var(--text-xs)', opacity: 0.7, fontFamily: 'var(--font-mono)' }}>{dateLabel(e)}</span>
        <button className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }}
                onClick={() => onViewDocument(e.document_id)}>
          {e.bates_begin}{e.title ? ` — ${e.title}` : ''}
        </button>
      </div>
      <div style={{ margin: '4px 0' }}>{e.description}</div>
      {e.participants.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {e.participants.map(p => (
            <button key={p.entity_id} className="btn btn-ghost btn-xs" onClick={() => openEntity(p.entity_id)}>
              <span className={`entity-dot entity-${p.entity_type}`} style={{ marginRight: 4 }}>●</span>
              {p.canonical_name}
            </button>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <div style={{ position: 'relative', height: '100dvh', display: 'flex', flexDirection: 'column' }}>
      <div className="panel-header" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn btn-ghost btn-xs" onClick={onBack}>← Back</button>
        <span style={{ fontWeight: 600 }}>Timeline ({total} events)</span>
        <select className="input" value={entityFilter} onChange={e => setEntityFilter(e.target.value)}
                style={{ marginLeft: 'auto', maxWidth: 240 }}>
          <option value="">All people & orgs</option>
          {filterOptions.map(o => <option key={o.id} value={o.id}>{o.canonical_name}</option>)}
        </select>
        <select className="input" value={typeFilter} onChange={e => setTypeFilter(e.target.value)} style={{ maxWidth: 150 }}>
          <option value="">All types</option>
          {Object.entries(TYPE_BADGES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)' }}>
        {groups.map(g => (
          <div key={g.key}>
            <div className="timeline-epoch">{g.key}</div>
            {g.items.map(renderEvent)}
          </div>
        ))}
        {events.length < total && (
          <button className="btn btn-xs" onClick={() => load(page + 1, true)}>Load more</button>
        )}
        {undatedCount > 0 && (
          <div style={{ marginTop: 16 }}>
            <button className="btn btn-ghost btn-xs" onClick={() => setShowUndated(v => !v)}>
              {showUndated ? '▾' : '▸'} Undated ({undatedCount})
            </button>
            {showUndated && undated.map(renderEvent)}
          </div>
        )}
        {total === 0 && (
          <div className="empty-state">
            {entityFilter || typeFilter
              ? 'No events match the current filters.'
              : 'No events extracted yet — run entity extraction from the Entities view.'}
          </div>
        )}
      </div>

      {openEntityId && (
        <EntityPanel entityId={openEntityId} onClose={() => openEntity(null)}
                     onOpenEntity={openEntity}
                     onOpenDocument={docId => { openEntity(null); onViewDocument(docId); }} />
      )}
    </div>
  );
}
