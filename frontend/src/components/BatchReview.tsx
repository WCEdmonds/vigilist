import { useEffect, useState } from 'react';
import { getBatch, listBatchDocuments, updateBatchDocument } from '../api/client';
import type { BatchDocument, ReviewBatch } from '../types';
import DocumentViewer from './DocumentViewer';

interface BatchReviewProps {
  batchId: number;
  onClose: () => void;
  onComplete: () => void;
}

export default function BatchReview({ batchId, onClose, onComplete }: BatchReviewProps) {
  const [batch, setBatch] = useState<ReviewBatch | null>(null);
  const [docs, setDocs] = useState<BatchDocument[]>([]);
  const [viewDocId, setViewDocId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [notification, setNotification] = useState<string | null>(null);

  const loadBatch = () => {
    setNotification(null);
    setLoading(true);
    Promise.all([getBatch(batchId), listBatchDocuments(batchId)])
      .then(([b, d]) => {
        setBatch(b);
        setDocs(d);
        // Auto-select first pending doc
        const firstPending = d.find(doc => doc.reviewed === 'pending');
        if (firstPending) setViewDocId(firstPending.document_id);
        else if (d.length > 0) setViewDocId(d[0].document_id);
      })
      .catch((e) => {
        setBatch(null);
        setDocs([]);
        setNotification(`Failed to load batch: ${e instanceof Error ? e.message : 'unknown error'}`);
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadBatch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchId]);

  const reviewedCount = docs.filter(d => d.reviewed !== 'pending').length;

  const advanceToNext = (currentDocId: string, updatedDocs: BatchDocument[]) => {
    const currentIdx = updatedDocs.findIndex(d => d.document_id === currentDocId);
    // Find next pending doc after current
    for (let i = currentIdx + 1; i < updatedDocs.length; i++) {
      if (updatedDocs[i].reviewed === 'pending') {
        setViewDocId(updatedDocs[i].document_id);
        return;
      }
    }
    // Wrap around: find any pending doc
    for (let i = 0; i < currentIdx; i++) {
      if (updatedDocs[i].reviewed === 'pending') {
        setViewDocId(updatedDocs[i].document_id);
        return;
      }
    }
    // No pending docs left — all done
  };

  const handleAction = async (status: 'reviewed' | 'skipped') => {
    if (!viewDocId || actionLoading) return;
    setActionLoading(true);
    try {
      const result = await updateBatchDocument(batchId, viewDocId, status);
      const updatedDocs = docs.map(d =>
        d.document_id === viewDocId ? { ...d, reviewed: status } : d
      );
      setDocs(updatedDocs);

      if (result.next_batch_id) {
        onComplete();
        return;
      }

      const allDone = updatedDocs.every(d => d.reviewed !== 'pending');
      if (allDone) {
        onComplete();
        return;
      }

      advanceToNext(viewDocId, updatedDocs);
    } catch {
      setNotification('Failed to save — please try again.');
    } finally {
      setActionLoading(false);
    }
  };

  const currentDoc = docs.find(d => d.document_id === viewDocId);
  const currentIndex = docs.findIndex(d => d.document_id === viewDocId);
  const allDone = docs.length > 0 && docs.every(d => d.reviewed !== 'pending');

  const statusColor = (reviewed: string) => {
    if (reviewed === 'reviewed') return 'var(--color-success-600)';
    if (reviewed === 'skipped') return 'var(--color-neutral-400)';
    return 'var(--color-primary-300)';
  };

  const statusLabel = (reviewed: string) => {
    if (reviewed === 'reviewed') return '✓';
    if (reviewed === 'skipped') return '—';
    return '·';
  };

  if (loading) {
    return (
      <div className="br-screen" style={{ alignItems: 'center', justifyContent: 'center' }}>
        <span className="spinner spinner-md" />
        <span style={{ marginLeft: 'var(--space-2)' }}>Loading batch…</span>
      </div>
    );
  }

  // Load failed — show an error screen with Retry rather than an empty sidebar.
  if (!batch && notification) {
    return (
      <div className="br-screen" style={{ flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 'var(--space-4)', padding: 'var(--space-6)' }}>
        <div style={{ fontSize: 'var(--text-lg)', fontFamily: 'var(--font-serif)', color: 'var(--color-ink)' }}>
          Could not load batch
        </div>
        <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)', textAlign: 'center', maxWidth: 480 }}>
          {notification}
        </div>
        <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
          <button className="btn btn-secondary" onClick={onClose}>Back to Batches</button>
          <button className="btn btn-primary" onClick={loadBatch}>Retry</button>
        </div>
      </div>
    );
  }

  return (
    <div className="br-screen" style={{ flexDirection: 'column' }}>
      {/* Header bar */}
      <div className="fullscreen-bar">
        <button className="cb-action" onClick={onClose}>← Back to Batches</button>
        <span className="fs-title">
          Batch Review{batch ? `: ${batch.queue_name}` : ''}
        </span>
        {currentDoc && (
          <span className="fs-progress">
            — doc {currentIndex + 1} of {docs.length}
          </span>
        )}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
          <span className="fs-progress">{reviewedCount}/{docs.length}</span>
          <progress value={reviewedCount} max={docs.length} style={{ width: 200 }} />
        </div>
      </div>

      {/* Notification banner */}
      {notification && (
        <div style={{ background: 'var(--color-primary-700)', color: 'var(--color-card)', padding: 'var(--space-2) var(--space-4)', fontSize: 'var(--text-sm)', textAlign: 'center', flexShrink: 0 }}>
          {notification}
        </div>
      )}

      {/* Body: sidebar + viewer */}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Sidebar */}
        <div style={{ width: 260, overflowY: 'auto', borderRight: '1px solid var(--color-neutral-200)', padding: 'var(--space-2)', flexShrink: 0, background: 'white' }}>
          <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', padding: 'var(--space-1) var(--space-2)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            Documents
          </div>
          {docs.map((doc, idx) => (
            <button
              key={doc.id}
              type="button"
              onClick={() => setViewDocId(doc.document_id)}
              style={{
                padding: 'var(--space-2) var(--space-2)',
                borderRadius: 'var(--radius-md)',
                cursor: 'pointer',
                background: viewDocId === doc.document_id ? 'var(--color-primary-50)' : 'transparent',
                borderTop: 'none',
                borderRight: 'none',
                borderBottom: 'none',
                borderLeft: viewDocId === doc.document_id ? '3px solid var(--color-primary-600)' : '3px solid transparent',
                marginBottom: 2,
                display: 'flex',
                alignItems: 'center',
                gap: 'var(--space-2)',
                width: '100%',
                textAlign: 'left',
                font: 'inherit',
                color: 'inherit',
              }}
            >
              <span style={{ fontSize: 11, fontWeight: 700, color: statusColor(doc.reviewed), width: 14, textAlign: 'center', flexShrink: 0 }}>
                {statusLabel(doc.reviewed)}
              </span>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 'var(--text-xs)', fontFamily: 'var(--font-mono)', color: 'var(--color-neutral-700)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {doc.bates_begin}
                </div>
                {doc.title && (
                  <div style={{ fontSize: 11, color: 'var(--color-neutral-400)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {doc.title}
                  </div>
                )}
                <div style={{ fontSize: 10, color: 'var(--color-neutral-400)' }}>#{idx + 1}</div>
              </div>
            </button>
          ))}
        </div>

        {/* Main content */}
        <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          {allDone ? (
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 'var(--space-4)' }}>
              <div style={{ fontSize: 'var(--text-xl)', fontFamily: 'var(--font-serif)', color: 'var(--color-neutral-700)' }}>
                Batch Complete
              </div>
              <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)' }}>
                All {docs.length} documents have been reviewed or skipped.
              </div>
              <button className="btn btn-primary" onClick={onComplete}>
                Return to Dashboard
              </button>
            </div>
          ) : viewDocId ? (
            <>
              {/* Action bar */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: 'var(--space-2) var(--space-3)', background: 'white', borderBottom: '1px solid var(--color-neutral-200)', flexShrink: 0 }}>
                <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', fontFamily: 'var(--font-mono)' }}>
                  {currentDoc?.bates_begin}
                </span>
                {currentDoc && currentDoc.reviewed !== 'pending' && (
                  <span style={{ fontSize: 'var(--text-xs)', color: statusColor(currentDoc.reviewed), fontWeight: 600, marginLeft: 'var(--space-1)' }}>
                    {currentDoc.reviewed === 'reviewed' ? 'Reviewed' : 'Skipped'}
                  </span>
                )}
                <div style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-2)' }}>
                  <button
                    className="btn btn-secondary btn-sm"
                    disabled={actionLoading}
                    onClick={() => handleAction('skipped')}
                  >
                    Skip
                  </button>
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={actionLoading}
                    onClick={() => handleAction('reviewed')}
                  >
                    {actionLoading ? 'Saving...' : 'Mark Reviewed'}
                  </button>
                </div>
              </div>

              {/* Document viewer */}
              <div style={{ flex: 1, overflow: 'hidden' }}>
                <DocumentViewer
                  docId={viewDocId}
                  onNavigate={setViewDocId}
                  onBack={() => setViewDocId(null)}
                  searchQuery=""
                  onSearch={() => {}}
                />
              </div>
            </>
          ) : (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--color-neutral-400)', fontSize: 'var(--text-sm)' }}>
              Select a document from the sidebar
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
