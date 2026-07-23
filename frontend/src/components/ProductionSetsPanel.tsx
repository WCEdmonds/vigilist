import { useCallback, useEffect, useState } from 'react';
import { listProductionSets, type ProductionSetInfo } from '../api/client';
import type { Tag } from '../types';
import ProductionBuilder from './ProductionBuilder';

interface Props {
  productionId: number;
  tags: Tag[];
  selectedIds: Set<string>;
  onOpenDoc: (id: string) => void;
}

function statusChip(s: ProductionSetInfo): string {
  if (s.status === 'draft') return 'Draft';
  if (s.package_status === 'packaged') return 'Packaged';
  if (s.package_status === 'packaging') return 'Packaging…';
  if (s.render_status === 'rendering') return `Rendering ${s.rendered_count}/${s.doc_count}`;
  if (s.render_status === 'error' || s.package_status === 'error') return 'Error';
  if (s.render_status === 'rendered') return 'Rendered';
  return 'Locked';
}

export default function ProductionSetsPanel({ productionId, tags, selectedIds, onOpenDoc }: Props) {
  const [sets, setSets] = useState<ProductionSetInfo[]>([]);
  const [builderSet, setBuilderSet] = useState<number | 'new' | null>(null);

  const refresh = useCallback(() => {
    listProductionSets(productionId).then(setSets).catch(e => console.warn('listProductionSets failed:', e));
  }, [productionId]);

  useEffect(() => { refresh(); }, [refresh]);

  const busy = sets.some(s => s.render_status === 'rendering' || s.package_status === 'packaging');
  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(refresh, 8000);
    return () => window.clearInterval(t);
  }, [busy, refresh]);

  return (
    <div className="card" style={{ marginBottom: 'var(--space-4)', padding: 'var(--space-4)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--space-3)' }}>
        <h2 className="section-title">
          Production Sets
          <span className="section-count">{sets.length}</span>
        </h2>
        <button className="btn btn-primary btn-sm" onClick={() => setBuilderSet('new')}>New production set</button>
      </div>
      {sets.length === 0 && (
        <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)' }}>
          No production sets yet. Build one to produce documents to another party.
        </div>
      )}
      {sets.map(s => (
        <button
          key={s.id}
          type="button"
          onClick={() => setBuilderSet(s.id)}
          style={{
            display: 'flex', width: '100%', justifyContent: 'space-between', alignItems: 'center',
            padding: 'var(--space-2) var(--space-3)', border: '1px solid rgba(44,62,107,0.1)',
            borderRadius: 'var(--radius-md)', marginBottom: 6, background: 'transparent',
            cursor: 'pointer', textAlign: 'left',
          }}
        >
          <span style={{ fontWeight: 600 }}>{s.name}</span>
          <span style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'center', fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)' }}>
            <span>{s.doc_count} docs</span>
            {s.bates_begin && <span>{s.bates_begin} – {s.bates_end}</span>}
            <span style={{ padding: '2px 8px', background: 'rgba(44,62,107,0.06)', borderRadius: 'var(--radius-sm)' }}>
              {statusChip(s)}
            </span>
          </span>
        </button>
      ))}
      {builderSet !== null && (
        <ProductionBuilder
          productionId={productionId}
          setId={builderSet}
          tags={tags}
          selectedIds={selectedIds}
          existingSets={sets}
          onOpenDoc={onOpenDoc}
          onClose={() => { setBuilderSet(null); refresh(); }}
        />
      )}
    </div>
  );
}
