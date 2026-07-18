import { useCallback, useEffect, useState } from 'react';
import {
  listQueues, createQueue, deleteQueue, createBatches, listQueueBatches, assignBatch, getProductionAccess,
  createQCSample,
} from '../api/client';
import type { ReviewQueue, ReviewBatch, ProductionAccessEntry } from '../types';
import QCReview from './QCReview';

interface Props {
  productionId: number;
  /** Bump this to force the queue list to refetch (e.g. after the AI lane
   * creates a queue from a slice). */
  refreshKey?: number;
}

interface BatchSizeState {
  [queueId: number]: string;
}

interface AssignState {
  [batchId: number]: string;
}

export default function HumanReviewLane({ productionId, refreshKey }: Props) {
  const [queues, setQueues] = useState<ReviewQueue[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Create queue form
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newQuery, setNewQuery] = useState('');
  const [creating, setCreating] = useState(false);

  // Per-queue expanded batches
  const [expandedQueues, setExpandedQueues] = useState<Set<number>>(new Set());
  const [batchesByQueue, setBatchesByQueue] = useState<Record<number, ReviewBatch[]>>({});

  // Batch size inputs per queue
  const [batchSizes, setBatchSizes] = useState<BatchSizeState>({});

  // Assign dropdown state: which batch is showing the dropdown
  const [assigningBatch, setAssigningBatch] = useState<number | null>(null);
  const [assignSelections, setAssignSelections] = useState<AssignState>({});
  const [accessUsers, setAccessUsers] = useState<ProductionAccessEntry[]>([]);

  // Delete confirmation
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);

  // QC state
  const [qcQueueId, setQcQueueId] = useState<number | null>(null);
  const [qcSamplePercent, setQcSamplePercent] = useState(10);
  const [showQcConfig, setShowQcConfig] = useState(false);
  const [qcSampleIds, setQcSampleIds] = useState<number[] | null>(null);
  const [qcLoading, setQcLoading] = useState(false);
  const [qcError, setQcError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [qs, users] = await Promise.all([
        listQueues(productionId),
        getProductionAccess(productionId),
      ]);
      setQueues(qs);
      setAccessUsers(users);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load queues');
    } finally {
      setLoading(false);
    }
  }, [productionId]);

  useEffect(() => { load(); }, [load, refreshKey]);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    setError('');
    try {
      await createQueue(productionId, newName.trim(), newDesc.trim(), newQuery.trim());
      setNewName('');
      setNewDesc('');
      setNewQuery('');
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to create queue');
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (queueId: number) => {
    setError('');
    try {
      await deleteQueue(productionId, queueId);
      setConfirmDelete(null);
      setExpandedQueues(prev => { const s = new Set(prev); s.delete(queueId); return s; });
      setBatchesByQueue(prev => { const n = { ...prev }; delete n[queueId]; return n; });
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to delete queue');
    }
  };

  const handleCreateBatches = async (queueId: number) => {
    const sizeStr = batchSizes[queueId] ?? '50';
    const size = Math.max(1, parseInt(sizeStr, 10) || 50);
    setError('');
    try {
      const batches = await createBatches(productionId, queueId, size);
      setBatchesByQueue(prev => ({ ...prev, [queueId]: batches }));
      setExpandedQueues(prev => new Set(prev).add(queueId));
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to create batches');
    }
  };

  const toggleExpand = async (queueId: number) => {
    setExpandedQueues(prev => {
      const next = new Set(prev);
      if (next.has(queueId)) {
        next.delete(queueId);
      } else {
        next.add(queueId);
      }
      return next;
    });

    // Fetch batches if expanding and not already loaded
    if (!expandedQueues.has(queueId)) {
      try {
        const batches = await listQueueBatches(productionId, queueId);
        setBatchesByQueue(prev => ({ ...prev, [queueId]: batches }));
      } catch {
        // non-critical — batch list will remain empty
      }
    }
  };

  const handleAssign = async (batchId: number) => {
    const reviewerId = assignSelections[batchId];
    if (!reviewerId) return;
    setError('');
    try {
      const updated = await assignBatch(batchId, reviewerId);
      // Update batches in state
      setBatchesByQueue(prev => {
        const entry = Object.entries(prev).find(([, batches]) =>
          batches.some(b => b.id === batchId)
        );
        if (!entry) return prev;
        const [qid, batches] = entry;
        return {
          ...prev,
          [qid]: batches.map(b => b.id === batchId ? updated : b),
        };
      });
      setAssigningBatch(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to assign batch');
    }
  };

  const handleOpenQcConfig = (queueId: number, e: React.MouseEvent) => {
    e.stopPropagation();
    setQcQueueId(queueId);
    setQcSamplePercent(10);
    setQcError('');
    setShowQcConfig(true);
  };

  const handleGenerateSample = async () => {
    if (!qcQueueId || qcLoading) return;
    setQcLoading(true);
    setQcError('');
    try {
      const ids = await createQCSample(qcQueueId, qcSamplePercent);
      if (ids.length === 0) {
        setQcError('No reviewed documents available for QC');
        return;
      }
      setShowQcConfig(false);
      setQcSampleIds(ids);
    } catch (e: unknown) {
      setQcError(e instanceof Error ? e.message : 'Failed to generate QC sample');
    } finally {
      setQcLoading(false);
    }
  };

  const statusBadgeClass = (status: string) => {
    switch (status) {
      case 'complete': return 'badge badge-green';
      case 'in_progress': return 'badge badge-blue';
      case 'pending': return 'badge badge-gray';
      default: return 'badge badge-gray';
    }
  };

  return (
    <div className="review-lane" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Lane toolbar */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: 'var(--space-2) var(--space-3)', borderBottom: '1px solid var(--color-neutral-200)',
      }}>
        <span style={{ fontSize: 'var(--text-sm)', fontWeight: 600, color: 'var(--color-neutral-500)' }}>Human Review</span>
      </div>

      {/* Create queue form */}
      <div style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--color-neutral-200)' }}>
        <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
          <input
            className="input input-sm"
            placeholder="Queue name *"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleCreate()}
            style={{ flex: '1 1 160px', minWidth: 120 }}
          />
          <input
            className="input input-sm"
            placeholder="Description (optional)"
            value={newDesc}
            onChange={e => setNewDesc(e.target.value)}
            style={{ flex: '2 1 200px', minWidth: 140 }}
          />
          <input
            className="input input-sm"
            placeholder="Search query (optional)"
            value={newQuery}
            onChange={e => setNewQuery(e.target.value)}
            style={{ flex: '2 1 200px', minWidth: 140 }}
          />
          <button
            className="btn btn-primary btn-sm"
            onClick={handleCreate}
            disabled={creating || !newName.trim()}
          >
            {creating ? 'Creating...' : 'Create'}
          </button>
        </div>
        {error && (
          <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-danger-700)' }}>
            {error}
          </div>
        )}
      </div>

      {/* Queue list */}
      <div style={{ overflowY: 'auto', flex: 1 }}>
        {loading && (
          <div className="loading-center" style={{ padding: 'var(--space-8)' }}>
            <span className="spinner spinner-md" />
            <span>Loading queues…</span>
          </div>
        )}

        {!loading && queues.length === 0 && (
          <div style={{ textAlign: 'center', padding: 'var(--space-8)', color: 'var(--color-neutral-400)', fontSize: 'var(--text-sm)' }}>
            No queues yet. Create one above.
          </div>
        )}

        {!loading && queues.map(queue => {
          const isExpanded = expandedQueues.has(queue.id);
          const batches = batchesByQueue[queue.id] ?? [];
          const progress = queue.total_documents > 0
            ? Math.round((queue.reviewed_documents / queue.total_documents) * 100)
            : 0;
          const isAiSlice = !!queue.filters?.ai;

          return (
            <div key={queue.id} style={{ borderBottom: '1px solid var(--color-neutral-200)' }}>
              {/* Queue row */}
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 'var(--space-3)',
                  padding: 'var(--space-3) var(--space-4)',
                  cursor: 'pointer',
                  background: isExpanded ? 'var(--color-neutral-50)' : 'white',
                }}
                onClick={() => toggleExpand(queue.id)}
              >
                <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', userSelect: 'none' }}>
                  {isExpanded ? '▼' : '▶'}
                </span>

                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                    <span style={{ fontWeight: 'var(--font-medium)', fontSize: 'var(--text-sm)' }}>
                      {queue.name}
                    </span>
                    <span className={statusBadgeClass(queue.status)} style={{ textTransform: 'capitalize' }}>
                      {queue.status.replace('_', ' ')}
                    </span>
                    {isAiSlice && <span className="queue-ai-badge">AI slice</span>}
                    <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', fontFamily: 'var(--font-mono)' }}>
                      {queue.batch_count} batch{queue.batch_count !== 1 ? 'es' : ''}
                    </span>
                  </div>
                  {queue.description && (
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginTop: 2 }}>
                      {queue.description}
                    </div>
                  )}
                  {/* Progress bar */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', marginTop: 'var(--space-1)' }}>
                    <div style={{ flex: 1, height: 4, background: 'var(--color-neutral-200)', borderRadius: 2, overflow: 'hidden' }}>
                      <div
                        style={{
                          height: '100%',
                          width: `${progress}%`,
                          background: progress === 100 ? 'var(--color-success-600)' : 'var(--color-primary-600)',
                          borderRadius: 2,
                          transition: 'width 0.3s',
                        }}
                      />
                    </div>
                    <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', fontFamily: 'var(--font-mono)', whiteSpace: 'nowrap' }}>
                      {queue.reviewed_documents}/{queue.total_documents}
                    </span>
                  </div>
                </div>

                {/* Per-queue actions */}
                <div
                  style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}
                  onClick={e => e.stopPropagation()}
                >
                  <input
                    className="input input-sm"
                    type="number"
                    min={1}
                    value={batchSizes[queue.id] ?? '50'}
                    onChange={e => setBatchSizes(prev => ({ ...prev, [queue.id]: e.target.value }))}
                    style={{ width: 64 }}
                    title="Batch size"
                  />
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => handleCreateBatches(queue.id)}
                  >
                    Create Batches
                  </button>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={e => handleOpenQcConfig(queue.id, e)}
                  >
                    Start QC
                  </button>
                  {confirmDelete === queue.id ? (
                    <>
                      <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-danger-700)' }}>Sure?</span>
                      <button
                        className="btn btn-sm"
                        style={{ background: 'var(--color-danger-600)', color: 'white' }}
                        onClick={() => handleDelete(queue.id)}
                      >
                        Yes
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => setConfirmDelete(null)}
                      >
                        No
                      </button>
                    </>
                  ) : (
                    <button
                      className="btn btn-ghost btn-sm"
                      style={{ color: 'var(--color-danger-600)' }}
                      onClick={() => setConfirmDelete(queue.id)}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>

              {/* Expanded batch list */}
              {isExpanded && (
                <div style={{ background: 'var(--color-neutral-50)', borderTop: '1px solid var(--color-neutral-100)' }}>
                  {batches.length === 0 ? (
                    <div style={{ padding: 'var(--space-4)', fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', textAlign: 'center' }}>
                      No batches yet — use "Create Batches" above to generate them.
                    </div>
                  ) : (
                    <table className="doc-table" style={{ fontSize: 'var(--text-xs)' }}>
                      <thead>
                        <tr>
                          <th>#</th>
                          <th>Reviewer</th>
                          <th>Status</th>
                          <th>Progress</th>
                          <th>Assign</th>
                        </tr>
                      </thead>
                      <tbody>
                        {batches.map((batch, idx) => (
                          <tr key={batch.id}>
                            <td className="meta-cell" style={{ fontFamily: 'var(--font-mono)' }}>
                              {idx + 1}
                            </td>
                            <td>
                              {batch.reviewer_email ?? (
                                <span style={{ color: 'var(--color-neutral-400)' }}>Unassigned</span>
                              )}
                            </td>
                            <td>
                              <span className={statusBadgeClass(batch.status)} style={{ textTransform: 'capitalize' }}>
                                {batch.status.replace('_', ' ')}
                              </span>
                            </td>
                            <td className="meta-cell" style={{ fontFamily: 'var(--font-mono)' }}>
                              {batch.reviewed_count}/{batch.size}
                            </td>
                            <td>
                              {!batch.reviewer_id ? (
                                assigningBatch === batch.id ? (
                                  <div
                                    style={{ display: 'flex', gap: 'var(--space-1)', alignItems: 'center' }}
                                    onClick={e => e.stopPropagation()}
                                  >
                                    <select
                                      className="input input-sm"
                                      value={assignSelections[batch.id] ?? ''}
                                      onChange={e => setAssignSelections(prev => ({ ...prev, [batch.id]: e.target.value }))}
                                      style={{ fontSize: 'var(--text-xs)' }}
                                    >
                                      <option value="">Select reviewer...</option>
                                      {accessUsers.map(u => (
                                        <option key={u.user_id} value={u.user_id}>
                                          {u.user_email}
                                        </option>
                                      ))}
                                    </select>
                                    <button
                                      className="btn btn-primary btn-sm"
                                      onClick={() => handleAssign(batch.id)}
                                      disabled={!assignSelections[batch.id]}
                                    >
                                      Assign
                                    </button>
                                    <button
                                      className="btn btn-ghost btn-sm"
                                      onClick={() => setAssigningBatch(null)}
                                    >
                                      Cancel
                                    </button>
                                  </div>
                                ) : (
                                  <button
                                    className="btn btn-ghost btn-sm"
                                    onClick={e => { e.stopPropagation(); setAssigningBatch(batch.id); }}
                                  >
                                    Assign
                                  </button>
                                )
                              ) : (
                                <span style={{ color: 'var(--color-neutral-400)' }}>—</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* QC config dialog */}
      {showQcConfig && (
        <div className="modal-overlay" onClick={() => { setShowQcConfig(false); setQcError(''); }}>
          <div
            className="modal-panel"
            onClick={e => e.stopPropagation()}
            style={{ maxWidth: 360, width: '90vw' }}
          >
            <div className="modal-header">
              <h3 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-base)' }}>Configure QC Sample</h3>
              <button className="btn btn-ghost btn-sm" onClick={() => { setShowQcConfig(false); setQcError(''); }}>Cancel</button>
            </div>
            <div style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
              <div>
                <label style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-600)', display: 'block', marginBottom: 4 }}>
                  Sample Percentage
                </label>
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                  <input
                    className="input input-sm"
                    type="number"
                    min={1}
                    max={100}
                    value={qcSamplePercent}
                    onChange={e => setQcSamplePercent(Math.max(1, Math.min(100, parseInt(e.target.value, 10) || 10)))}
                    style={{ width: 80 }}
                  />
                  <span style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)' }}>% of reviewed documents</span>
                </div>
              </div>
              {qcError && (
                <div style={{ color: 'var(--color-danger-700)', fontSize: 'var(--text-xs)', background: 'var(--color-danger-50)', padding: 'var(--space-2)', borderRadius: 'var(--radius-md)' }}>
                  {qcError}
                </div>
              )}
              <button
                className="btn btn-primary btn-sm"
                disabled={qcLoading}
                onClick={handleGenerateSample}
              >
                {qcLoading ? 'Generating...' : 'Generate Sample'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* QC Review overlay */}
      {qcSampleIds && qcQueueId !== null && (
        <QCReview
          sampleIds={qcSampleIds}
          productionId={productionId}
          onClose={() => { setQcSampleIds(null); setQcQueueId(null); }}
        />
      )}
    </div>
  );
}
