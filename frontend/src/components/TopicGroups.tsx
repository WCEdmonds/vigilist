import type { ClusterInfo } from '../types';

interface Props {
  clusters: ClusterInfo[];
  activeClusterId: number | null;
  onSelect: (clusterId: number | null) => void;
}

export default function TopicGroups({ clusters, activeClusterId, onSelect }: Props) {
  if (clusters.length === 0) return null;

  return (
    <div style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'rgba(44,62,107,0.5)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 'var(--space-2)' }}>
        Topics
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)' }}>
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
              {c.doc_count}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
