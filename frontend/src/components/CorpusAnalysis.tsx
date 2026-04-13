import { useEffect, useState } from 'react';
import { clusterProduction, getClusters } from '../api/client';
import type { ClusterInfo } from '../types';

interface ClusterDetail extends ClusterInfo {
  key_documents?: { id: string; bates_begin: string; title: string | null; page_count: number }[];
}

interface Props {
  productionId: number;
  onViewDocument: (docId: string) => void;
  onFilterCluster: (clusterId: number) => void;
  onBack: () => void;
}

const TOPIC_COLORS = [
  '#4f46e5', '#0891b2', '#059669', '#d97706', '#dc2626',
  '#7c3aed', '#2563eb', '#0d9488', '#65a30d', '#ea580c',
  '#9333ea', '#0284c7', '#16a34a', '#ca8a04', '#e11d48',
  '#6d28d9', '#0369a1', '#15803d', '#b45309', '#be123c',
];

function DonutChart({ data, colors, size = 200 }: { data: { label: string; value: number }[]; colors: string[]; size?: number }) {
  const total = data.reduce((s, d) => s + d.value, 0);
  if (total === 0) return null;

  const cx = size / 2;
  const cy = size / 2;
  const outerR = size / 2 - 2;
  const innerR = outerR * 0.55;
  let startAngle = -Math.PI / 2;

  const slices = data.map((d, i) => {
    const angle = (d.value / total) * 2 * Math.PI;
    const endAngle = startAngle + angle;
    const largeArc = angle > Math.PI ? 1 : 0;

    const x1 = cx + outerR * Math.cos(startAngle);
    const y1 = cy + outerR * Math.sin(startAngle);
    const x2 = cx + outerR * Math.cos(endAngle);
    const y2 = cy + outerR * Math.sin(endAngle);
    const ix1 = cx + innerR * Math.cos(endAngle);
    const iy1 = cy + innerR * Math.sin(endAngle);
    const ix2 = cx + innerR * Math.cos(startAngle);
    const iy2 = cy + innerR * Math.sin(startAngle);

    const path = `M ${x1} ${y1} A ${outerR} ${outerR} 0 ${largeArc} 1 ${x2} ${y2} L ${ix1} ${iy1} A ${innerR} ${innerR} 0 ${largeArc} 0 ${ix2} ${iy2} Z`;

    startAngle = endAngle;
    return <path key={i} d={path} fill={colors[i % colors.length]} opacity={0.85} />;
  });

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      {slices}
      <text x={cx} y={cy - 6} textAnchor="middle" fontSize="20" fontWeight="700" fill="var(--color-ink)">{total}</text>
      <text x={cx} y={cy + 12} textAnchor="middle" fontSize="10" fill="var(--color-neutral-400)">documents</text>
    </svg>
  );
}

function BarChart({ data, colors, onClickBar }: { data: { label: string; value: number; id: number }[]; colors: string[]; onClickBar: (id: number) => void }) {
  const maxVal = Math.max(...data.map(d => d.value), 1);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {data.map((d, i) => (
        <button
          key={d.id}
          type="button"
          onClick={() => onClickBar(d.id)}
          aria-label={`${d.label}: ${d.value} documents`}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--space-2)',
            cursor: 'pointer',
            background: 'transparent',
            border: 'none',
            padding: 0,
            textAlign: 'left',
            font: 'inherit',
            color: 'inherit',
            width: '100%',
          }}
        >
          <div style={{ width: 160, fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right', flexShrink: 0 }}>
            {d.label}
          </div>
          <div style={{ flex: 1, height: 20, background: 'var(--color-neutral-100)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{
              height: '100%', width: `${(d.value / maxVal) * 100}%`,
              background: colors[i % colors.length], borderRadius: 3,
              transition: 'width 0.3s ease',
              display: 'flex', alignItems: 'center', justifyContent: 'flex-end', paddingRight: 6,
            }}>
              {d.value > maxVal * 0.15 && (
                <span style={{ fontSize: 10, color: 'var(--color-card)', fontWeight: 600 }}>{d.value}</span>
              )}
            </div>
          </div>
          {d.value <= maxVal * 0.15 && (
            <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', width: 24, flexShrink: 0 }}>{d.value}</span>
          )}
        </button>
      ))}
    </div>
  );
}

export default function CorpusAnalysis({ productionId, onViewDocument, onFilterCluster, onBack }: Props) {
  const [clusters, setClusters] = useState<ClusterDetail[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const [expandedCluster, setExpandedCluster] = useState<number | null>(null);

  useEffect(() => {
    loadClusters();
  }, [productionId]);

  const loadClusters = async () => {
    setLoading(true);
    try {
      const data = await getClusters(productionId);
      setClusters(data);
    } catch {
      setClusters([]);
    } finally {
      setLoading(false);
    }
  };

  const runClustering = async () => {
    setRunning(true);
    setError('');
    try {
      const result = await clusterProduction(productionId);
      if (result.clusters) {
        setClusters(result.clusters);
      } else {
        await loadClusters();
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  };

  const sorted = [...clusters].sort((a, b) => b.doc_count - a.doc_count);
  const totalDocs = clusters.reduce((sum, c) => sum + c.doc_count, 0);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      {/* Header */}
      <div className="app-header">
        <button className="btn-header" onClick={onBack}>← Back</button>
        <span className="logo">Production Analysis</span>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-5)' }}>
        {loading && (
          <div className="loading-center"><span className="spinner spinner-md" /> Loading analysis…</div>
        )}

        {!loading && clusters.length === 0 && !running && (
          <div style={{ textAlign: 'center', padding: 'var(--space-8)', maxWidth: 600, margin: '0 auto' }}>
            <h2 style={{ fontFamily: 'var(--font-serif)', fontSize: 'var(--text-xl)', marginBottom: 'var(--space-3)' }}>
              Corpus Analysis
            </h2>
            <p style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-4)', lineHeight: 1.6 }}>
              Analyze all documents in this production using AI. The system will cluster documents by topic
              and label each cluster. Topics will appear as filters on the main document list.
            </p>
            <button className="btn btn-primary" onClick={runClustering} style={{ padding: '10px 32px' }}>
              Run Analysis
            </button>
            {error && (
              <div style={{ marginTop: 'var(--space-3)', color: 'var(--color-danger-600)', fontSize: 'var(--text-sm)' }}>
                {error}
              </div>
            )}
          </div>
        )}

        {running && (
          <div style={{ textAlign: 'center', padding: 'var(--space-8)' }}>
            <div className="spinner spinner-md" style={{ margin: '0 auto var(--space-4)' }} />
            <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-600)', fontWeight: 500 }}>
              Clustering documents and labeling topics...
            </div>
            <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', marginTop: 'var(--space-2)' }}>
              This may take up to a minute
            </div>
          </div>
        )}

        {!loading && clusters.length > 0 && (
          <>
            {/* Charts section */}
            <div style={{
              display: 'flex', gap: 'var(--space-5)', marginBottom: 'var(--space-5)',
              padding: 'var(--space-4)', background: 'white',
              border: '1px solid var(--color-neutral-200)', borderRadius: 'var(--radius-xl)',
              alignItems: 'flex-start', flexWrap: 'wrap',
            }}>
              {/* Donut */}
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 'var(--space-2)' }}>
                <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                  Distribution
                </div>
                <DonutChart
                  data={sorted.map(c => ({ label: c.label || `Cluster ${c.cluster_index + 1}`, value: c.doc_count }))}
                  colors={TOPIC_COLORS}
                  size={180}
                />
              </div>

              {/* Bar chart */}
              <div style={{ flex: 1, minWidth: 300 }}>
                <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 'var(--space-2)' }}>
                  Documents by Topic
                </div>
                <BarChart
                  data={sorted.map((c) => ({
                    label: c.label || `Cluster ${c.cluster_index + 1}`,
                    value: c.doc_count,
                    id: c.id,
                  }))}
                  colors={TOPIC_COLORS}
                  onClickBar={(id) => onFilterCluster(id)}
                />
              </div>

              {/* Legend */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 120 }}>
                <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 'var(--space-1)' }}>
                  Summary
                </div>
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-ink)' }}>
                  <strong>{totalDocs}</strong> documents
                </div>
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-ink)' }}>
                  <strong>{clusters.length}</strong> topics
                </div>
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-ink)' }}>
                  <strong>{Math.round(totalDocs / clusters.length)}</strong> avg per topic
                </div>
              </div>
            </div>

            {/* Topic clusters list */}
            <h3 style={{ fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)', marginBottom: 'var(--space-2)' }}>
              Topic Clusters
            </h3>
            <p style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-400)', marginBottom: 'var(--space-3)' }}>
              Click a topic name to filter the document list. Expand to browse documents within each topic.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
              {sorted.map((cluster, i) => (
                <div key={cluster.id} style={{
                  background: 'white', border: '1px solid var(--color-neutral-200)',
                  borderRadius: 'var(--radius-lg)', overflow: 'hidden',
                }}>
                  <div style={{
                    padding: 'var(--space-3) var(--space-4)',
                    display: 'flex', alignItems: 'center', gap: 'var(--space-3)',
                    borderLeft: `4px solid ${TOPIC_COLORS[i % TOPIC_COLORS.length]}`,
                  }}>
                    <div style={{
                      width: 36, height: 36, borderRadius: '50%', display: 'flex',
                      alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: 'var(--text-sm)',
                      background: TOPIC_COLORS[i % TOPIC_COLORS.length] + '15',
                      color: TOPIC_COLORS[i % TOPIC_COLORS.length],
                      flexShrink: 0,
                    }}>
                      {cluster.doc_count}
                    </div>
                    {cluster.page_count > 0 && (
                      <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', flexShrink: 0 }}>
                        ({cluster.page_count} pp.)
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() => onFilterCluster(cluster.id)}
                      style={{
                        flex: 1,
                        cursor: 'pointer',
                        background: 'transparent',
                        border: 'none',
                        padding: 0,
                        textAlign: 'left',
                        font: 'inherit',
                        color: 'inherit',
                      }}
                    >
                      <div style={{ fontSize: 'var(--text-sm)', fontWeight: 600 }}>
                        {cluster.label || `Cluster ${cluster.cluster_index + 1}`}
                      </div>
                      <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)' }}>
                        Click to filter documents
                      </div>
                    </button>
                    <button
                      className="btn btn-ghost btn-xs"
                      onClick={() => setExpandedCluster(expandedCluster === cluster.id ? null : cluster.id)}
                    >
                      {expandedCluster === cluster.id ? '▲ Collapse' : '▼ Expand'}
                    </button>
                  </div>

                  {expandedCluster === cluster.id && cluster.key_documents && (
                    <div style={{ padding: '0 var(--space-4) var(--space-3)', borderTop: '1px solid var(--color-neutral-100)' }}>
                      {cluster.key_documents.map(d => (
                        <button
                          key={d.id}
                          type="button"
                          onClick={() => onViewDocument(d.id)}
                          style={{
                            padding: 'var(--space-1-5) 0', cursor: 'pointer',
                            borderTop: 'none',
                            borderLeft: 'none',
                            borderRight: 'none',
                            borderBottom: '1px solid var(--color-neutral-50)',
                            display: 'flex', alignItems: 'center', gap: 'var(--space-2)',
                            background: 'transparent',
                            width: '100%',
                            textAlign: 'left',
                            font: 'inherit',
                            color: 'inherit',
                          }}
                        >
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ fontSize: 'var(--text-sm)', fontWeight: 500 }}>{d.title || d.bates_begin}</div>
                            <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)' }}>
                              {d.bates_begin} · {d.page_count} pg{d.page_count !== 1 ? 's' : ''}
                            </div>
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
