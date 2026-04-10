import { useState } from 'react';
import type { ClusterInfo } from '../types';

interface Props {
  clusters: ClusterInfo[];
  activeClusterId: number | null;
  onSelect: (clusterId: number | null) => void;
  onOpenAnalysis?: () => void;
}

export default function TopicGroups({ clusters, activeClusterId, onSelect, onOpenAnalysis }: Props) {
  const [expanded, setExpanded] = useState(false);

  if (clusters.length === 0 && !onOpenAnalysis) return null;

  const activeLabel = activeClusterId !== null
    ? clusters.find(c => c.id === activeClusterId)?.label || 'Unknown'
    : null;

  return (
    <div style={{ marginBottom: 'var(--space-3)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          style={{
            display: 'flex', alignItems: 'center', gap: 'var(--space-2)',
            background: 'none', border: 'none', cursor: 'pointer', padding: 0,
          }}
        >
          <span style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'rgba(44,62,107,0.5)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            Clusters <span style={{ textTransform: 'none', fontWeight: 400, opacity: 0.6 }}>(beta)</span>
          </span>
          <span style={{ fontSize: 10, color: 'rgba(44,62,107,0.3)', transition: 'transform 0.2s', transform: expanded ? 'rotate(90deg)' : 'rotate(0)' }}>
            ▶
          </span>
        </button>
        {activeLabel && !expanded && (
          <span className="badge" style={{ fontSize: 11, border: '2px solid var(--color-ink)', background: 'rgba(44,62,107,0.08)', color: 'var(--color-ink)', fontWeight: 700, padding: '2px 8px' }}>
            {activeLabel}
            <button
              type="button"
              className="badge-remove"
              aria-label="Clear cluster filter"
              onClick={() => onSelect(null)}
            >
              &times;
            </button>
          </span>
        )}
      </div>

      {expanded && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', marginTop: 'var(--space-2)', alignItems: 'center' }}>
          {onOpenAnalysis && (
            <button
              onClick={onOpenAnalysis}
              className="badge"
              style={{
                cursor: 'pointer',
                border: '1px solid rgba(44,62,107,0.15)',
                background: 'var(--color-card)',
                color: 'var(--color-ink)',
                padding: '4px 10px',
                fontSize: 12,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <span className="ai-indicator" style={{ padding: '0 4px', fontSize: 9 }}>AI</span>
              Production Analysis
            </button>
          )}
          {activeClusterId !== null && (
            <button className="btn btn-ghost btn-xs" onClick={() => onSelect(null)} style={{ fontSize: 11 }}>
              Clear filter
            </button>
          )}
          {clusters.map(c => (
            <button
              key={c.id}
              onClick={() => onSelect(activeClusterId === c.id ? null : c.id)}
              className="badge"
              style={{
                cursor: 'pointer',
                border: activeClusterId === c.id ? '2px solid var(--color-ink)' : '1px solid rgba(44,62,107,0.15)',
                background: activeClusterId === c.id ? 'rgba(44,62,107,0.08)' : 'var(--color-card)',
                color: 'var(--color-ink)',
                padding: '4px 10px',
                fontSize: 12,
                fontWeight: activeClusterId === c.id ? 700 : 400,
              }}
            >
              {c.label || `Cluster ${c.cluster_index + 1}`}
              <span style={{ marginLeft: 4, opacity: 0.4, fontFamily: 'var(--font-mono)', fontSize: 10 }}>
                {c.doc_count}{c.page_count > 0 ? ` (${c.page_count} pp.)` : ''}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
