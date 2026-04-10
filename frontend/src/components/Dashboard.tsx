import { useCallback, useEffect, useState } from 'react';
import { getDashboard, getQCStats } from '../api/client';
import type { DashboardStats, QCStats } from '../types';

interface Props {
  productionId: number;
  onClose: () => void;
}

export default function Dashboard({ productionId, onClose }: Props) {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [qcStats, setQcStats] = useState<QCStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const handleRefresh = useCallback(() => {
    setRefreshKey(k => k + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const [d, q] = await Promise.all([
          getDashboard(productionId),
          getQCStats(productionId),
        ]);
        if (!cancelled) { setStats(d); setQcStats(q); }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load dashboard');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    run();
    return () => { cancelled = true; };
  }, [productionId, refreshKey]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel modal-large" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>Review Dashboard</h2>
          <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
            <button className="btn btn-secondary btn-sm" onClick={handleRefresh} disabled={loading}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
            <button className="modal-close-btn" aria-label="Close" onClick={onClose}>&times;</button>
          </div>
        </div>

        <div className="modal-body">
        {loading && (
          <div className="loading-center">
            <span className="spinner spinner-md" />
            <span>Loading review stats…</span>
          </div>
        )}

        {error && !loading && (
          <div style={{ padding: 'var(--space-6)', textAlign: 'center', color: 'var(--color-error)' }}>
            <div style={{ marginBottom: 'var(--space-3)' }}>{error}</div>
            <button className="btn btn-secondary btn-sm" onClick={handleRefresh}>Retry</button>
          </div>
        )}

        {!loading && !error && stats && qcStats && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6)' }}>

            {/* Overall Progress */}
            <section>
              <h3 style={{ fontSize: 'var(--text-sm)', fontWeight: 600, marginBottom: 'var(--space-3)', color: 'var(--color-neutral-600)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                Overall Progress
              </h3>
              <div style={{ marginBottom: 'var(--space-3)' }}>
                <span style={{ fontSize: 'var(--text-3xl)', fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--color-primary-900)' }}>
                  {(stats.percent_complete || 0).toFixed(1)}%
                </span>
              </div>
              <progress value={stats.reviewed_documents} max={stats.total_documents} style={{ width: '100%', marginBottom: 'var(--space-3)' }} />
              <div style={{ display: 'flex', gap: 'var(--space-6)' }}>
                <div>
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginBottom: 2 }}>Total</div>
                  <div style={{ fontSize: 'var(--text-lg)', fontWeight: 600, fontFamily: 'var(--font-mono)' }}>{stats.total_documents.toLocaleString()}</div>
                </div>
                <div>
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginBottom: 2 }}>Reviewed</div>
                  <div style={{ fontSize: 'var(--text-lg)', fontWeight: 600, fontFamily: 'var(--font-mono)', color: 'var(--color-success)' }}>{stats.reviewed_documents.toLocaleString()}</div>
                </div>
                <div>
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginBottom: 2 }}>Pending</div>
                  <div style={{ fontSize: 'var(--text-lg)', fontWeight: 600, fontFamily: 'var(--font-mono)', color: 'var(--color-warning)' }}>{stats.pending_documents.toLocaleString()}</div>
                </div>
              </div>
            </section>

            {/* Queue Breakdown */}
            {stats.queue_stats.length > 0 && (
              <section>
                <h3 style={{ fontSize: 'var(--text-sm)', fontWeight: 600, marginBottom: 'var(--space-3)', color: 'var(--color-neutral-600)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  Queue Breakdown
                </h3>
                <div style={{ overflowX: 'auto' }}>
                  <table className="doc-table">
                    <thead>
                      <tr>
                        <th>Queue Name</th>
                        <th style={{ textAlign: 'right' }}>Total Docs</th>
                        <th style={{ textAlign: 'right' }}>Reviewed</th>
                        <th style={{ textAlign: 'right' }}>Batch Count</th>
                        <th style={{ textAlign: 'right' }}>% Complete</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stats.queue_stats.map(q => {
                        const pct = q.total > 0 ? (q.reviewed / q.total) * 100 : 0;
                        return (
                          <tr key={q.queue_id}>
                            <td>{q.name}</td>
                            <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{q.total.toLocaleString()}</td>
                            <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{q.reviewed.toLocaleString()}</td>
                            <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{q.batch_count}</td>
                            <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{pct.toFixed(1)}%</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </section>
            )}

            {/* Reviewer Stats */}
            {stats.reviewer_stats.length > 0 && (
              <section>
                <h3 style={{ fontSize: 'var(--text-sm)', fontWeight: 600, marginBottom: 'var(--space-3)', color: 'var(--color-neutral-600)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  Reviewer Stats
                </h3>
                <div style={{ overflowX: 'auto' }}>
                  <table className="doc-table">
                    <thead>
                      <tr>
                        <th>Reviewer</th>
                        <th style={{ textAlign: 'right' }}>Documents Reviewed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stats.reviewer_stats.map(r => (
                        <tr key={r.user_id}>
                          <td>{r.email}</td>
                          <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{r.reviewed_count.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}

            {/* Tag Distribution */}
            {Object.keys(stats.tag_breakdown).length > 0 && (
              <section>
                <h3 style={{ fontSize: 'var(--text-sm)', fontWeight: 600, marginBottom: 'var(--space-3)', color: 'var(--color-neutral-600)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  Tag Distribution
                </h3>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
                  {Object.entries(stats.tag_breakdown).map(([category, tags]) => {
                    const maxCount = Object.values(tags).length > 0 ? Math.max(...Object.values(tags)) : 1;
                    return (
                    <div key={category}>
                      <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', marginBottom: 'var(--space-2)', textTransform: 'uppercase' }}>
                        {category}
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
                        {Object.entries(tags).map(([tagName, count]) => {
                          const barWidth = maxCount > 0 ? (count / maxCount) * 100 : 0;
                          return (
                            <div key={tagName} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
                              <div style={{ width: 140, fontSize: 'var(--text-xs)', color: 'var(--color-neutral-700)', flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {tagName}
                              </div>
                              <div style={{ flex: 1, height: 8, background: 'var(--color-neutral-100)', borderRadius: 4, overflow: 'hidden' }}>
                                <div style={{ width: `${barWidth}%`, height: '100%', background: 'var(--color-primary-600)', borderRadius: 4, transition: 'width 0.3s ease' }} />
                              </div>
                              <div style={{ width: 48, textAlign: 'right', fontSize: 'var(--text-xs)', fontFamily: 'var(--font-mono)', color: 'var(--color-neutral-600)', flexShrink: 0 }}>
                                {count.toLocaleString()}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  ); })}
                </div>
              </section>
            )}

            {/* QC Section */}
            {qcStats.total_decisions > 0 && (
              <section>
                <h3 style={{ fontSize: 'var(--text-sm)', fontWeight: 600, marginBottom: 'var(--space-3)', color: 'var(--color-neutral-600)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  QC Summary
                </h3>
                <div style={{ display: 'flex', gap: 'var(--space-6)', marginBottom: 'var(--space-4)' }}>
                  <div>
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginBottom: 2 }}>Total Decisions</div>
                    <div style={{ fontSize: 'var(--text-lg)', fontWeight: 600, fontFamily: 'var(--font-mono)' }}>{qcStats.total_decisions.toLocaleString()}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginBottom: 2 }}>Agree</div>
                    <div style={{ fontSize: 'var(--text-lg)', fontWeight: 600, fontFamily: 'var(--font-mono)', color: 'var(--color-success)' }}>{qcStats.agree_count.toLocaleString()}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginBottom: 2 }}>Overturned</div>
                    <div style={{ fontSize: 'var(--text-lg)', fontWeight: 600, fontFamily: 'var(--font-mono)', color: 'var(--color-error)' }}>{qcStats.overturn_count.toLocaleString()}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginBottom: 2 }}>Overturn Rate</div>
                    <div style={{ fontSize: 'var(--text-lg)', fontWeight: 600, fontFamily: 'var(--font-mono)', color: qcStats.overturn_rate > 10 ? 'var(--color-error)' : 'var(--color-neutral-700)' }}>
                      {qcStats.overturn_rate.toFixed(1)}%
                    </div>
                  </div>
                </div>

                {qcStats.by_reviewer.length > 0 && (
                  <div style={{ overflowX: 'auto' }}>
                    <table className="doc-table">
                      <thead>
                        <tr>
                          <th>Reviewer</th>
                          <th style={{ textAlign: 'right' }}>Total</th>
                          <th style={{ textAlign: 'right' }}>Overturns</th>
                          <th style={{ textAlign: 'right' }}>Overturn Rate</th>
                        </tr>
                      </thead>
                      <tbody>
                        {qcStats.by_reviewer.map(r => (
                          <tr key={r.reviewer_id}>
                            <td>{r.email}</td>
                            <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{r.total.toLocaleString()}</td>
                            <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>{r.overturns.toLocaleString()}</td>
                            <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', color: r.overturn_rate > 10 ? 'var(--color-error)' : undefined }}>
                              {r.overturn_rate.toFixed(1)}%
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            )}

          </div>
        )}
        </div>
      </div>
    </div>
  );
}
