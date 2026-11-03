import { useEffect, useState } from 'react';
import { getAiEstimate } from '../api/client';
import { announceAiRunStarted, formatUsd } from '../utils/aiActivity';
import type { AiEstimate } from '../types';

export interface AiRunRequest {
  /** 'rebuild' prices a full re-extraction (every document with text). */
  mode?: 'pending' | 'rebuild';
  title?: string;
  /** Extra caution shown above the estimate (e.g. what a rebuild deletes). */
  warning?: string;
  /** Starts the run. Runs only after the user clicks Run. */
  run: () => Promise<void> | void;
}

interface Props {
  productionId: number;
  request: AiRunRequest;
  onClose: () => void;
}

/**
 * The one confirmation shown before anything starts paid AI analysis:
 * fetches the cost estimate and enqueues nothing until Run is clicked.
 */
export default function AiRunConfirm({ productionId, request, onClose }: Props) {
  const mode = request.mode ?? 'pending';
  const [estimate, setEstimate] = useState<AiEstimate | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getAiEstimate(productionId, { force: true, mode })
      .then(e => { if (!cancelled) setEstimate(e); })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'unknown error'); });
    return () => { cancelled = true; };
  }, [productionId, mode]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, busy]);

  const notAllowed = estimate !== null && !estimate.can_run;
  const canRun = !busy && !notAllowed && (estimate !== null || error !== null);

  const handleRun = async () => {
    setBusy(true);
    try {
      await request.run();
      announceAiRunStarted();
    } finally {
      onClose();
    }
  };

  const docs = estimate?.documents_pending ?? 0;

  return (
    <div className="modal-overlay" onClick={() => { if (!busy) onClose(); }}>
      <div className="modal-panel" role="dialog" aria-modal="true" aria-labelledby="ai-run-confirm-title"
           onClick={e => e.stopPropagation()} style={{ maxWidth: 460 }}>
        <div className="modal-header">
          <h2 className="modal-title" id="ai-run-confirm-title">
            <span className="brief-ai-mark">✦</span> {request.title ?? 'Run AI analysis?'}
          </h2>
          <button className="modal-close-btn" aria-label="Close" onClick={onClose} disabled={busy}>&times;</button>
        </div>
        <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)', fontSize: 'var(--text-sm)' }}>
          {request.warning && <div style={{ color: 'var(--color-neutral-700)' }}>{request.warning}</div>}
          {estimate === null && error === null && (
            <div style={{ color: 'var(--color-neutral-500)' }} aria-live="polite">Estimating cost…</div>
          )}
          {estimate !== null && (
            <div aria-live="polite">
              This will run AI analysis on {docs.toLocaleString()} document{docs === 1 ? '' : 's'}.
              {' '}Estimated cost: <strong>{formatUsd(estimate.usd_estimate)}</strong>. Continue?
              <div style={{ marginTop: 4, fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>
                Covers per-document summaries and entity extraction at batch pricing.
              </div>
            </div>
          )}
          {error !== null && (
            <div role="alert" style={{ color: 'var(--color-neutral-700)' }}>
              Couldn't estimate the cost ({error}). You can still run it.
            </div>
          )}
          {notAllowed && (
            <div role="alert" style={{ color: 'var(--color-neutral-500)' }}>
              Only a matter manager or admin can run AI analysis.
            </div>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button className="btn btn-ghost btn-sm" onClick={onClose} disabled={busy}>Cancel</button>
            <button className="btn btn-primary btn-sm" disabled={!canRun} onClick={handleRun}>
              {busy ? 'Starting…' : 'Run'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
