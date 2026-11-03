import { useCallback, useEffect, useState } from 'react';
import { getAiEstimate, getIngestAwaiting, runPipeline } from '../api/client';
import { showToast } from './Toast';
import type { AiEstimate, AwaitingIngest } from '../types';

const POLL_MS = 15000;

interface Props {
  productionId: number;
  /** Reopen the ingest wizard on a load that is waiting for its selection. */
  onResumeIngest?: (jobId: string) => void;
  /** Called after analysis is started so the brief refreshes. */
  onAnalysisStarted?: () => void;
}

/**
 * Matter-level notices: a large mailbox load waiting for the uploader's choice,
 * and the sticky AI hold (analysis not run, with its cost and a Run button).
 */
export default function MatterNotices({ productionId, onResumeIngest, onAnalysisStarted }: Props) {
  const [awaiting, setAwaiting] = useState<AwaitingIngest[]>([]);
  const [estimate, setEstimate] = useState<AiEstimate | null>(null);
  const [running, setRunning] = useState(false);

  // Only the awaiting-selection check is polled. The AI estimate scans the
  // pending text server-side, so it is fetched once per mount and after Run.
  const loadAwaiting = useCallback(async () => {
    try {
      setAwaiting(await getIngestAwaiting(productionId));
    } catch {
      // Transient: the next poll retries.
    }
  }, [productionId]);

  const loadEstimate = useCallback(async () => {
    try {
      setEstimate(await getAiEstimate(productionId));
    } catch {
      // Leave the banner as it was.
    }
  }, [productionId]);

  useEffect(() => {
    let cancelled = false;
    const tick = () => { if (!cancelled) void loadAwaiting(); };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [loadAwaiting]);

  useEffect(() => {
    void loadEstimate();
  }, [loadEstimate]);

  const handleRun = async () => {
    setRunning(true);
    try {
      await runPipeline(productionId, false);
      setEstimate(prev => (prev ? { ...prev, held: false } : prev));
      void loadEstimate();
      onAnalysisStarted?.();
      showToast('AI analysis started', 'success');
    } catch (e) {
      showToast(`Could not start AI analysis: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
    } finally {
      setRunning(false);
    }
  };

  const noticeStyle: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 'var(--space-3)', flexWrap: 'wrap',
    padding: 'var(--space-2) var(--space-3)', marginBottom: 'var(--space-3)',
    background: 'var(--color-brass-soft)', border: '1px solid rgba(20, 24, 29, 0.1)',
    borderRadius: 'var(--radius-md)', fontSize: 'var(--text-sm)', color: 'var(--color-neutral-700)',
  };

  return (
    <>
      {awaiting.map(a => (
        <div key={a.job_id} style={noticeStyle} role="status">
          <span style={{ flex: 1 }}>
            <strong>{a.name}</strong> is waiting for you to choose what to bring in.
          </span>
          <button className="btn btn-primary btn-sm" onClick={() => onResumeIngest?.(a.job_id)}>
            Choose what to bring in
          </button>
        </div>
      ))}
      {estimate?.held && (
        <div style={noticeStyle} role="status">
          <span style={{ flex: 1 }}>
            <span className="brief-ai-mark">✦</span> AI analysis not run
            {estimate.documents_pending > 0 && <> for {estimate.documents_pending.toLocaleString()} documents</>}
            {' '}— estimated ${estimate.usd_estimate.toFixed(2)}
          </span>
          {estimate.can_run ? (
            <button className="btn btn-primary btn-sm" disabled={running} onClick={handleRun}>
              {running ? 'Starting…' : 'Run'}
            </button>
          ) : (
            <span style={{ color: 'var(--color-neutral-500)' }}>AI analysis is on hold: ask a matter manager to run it.</span>
          )}
        </div>
      )}
    </>
  );
}
