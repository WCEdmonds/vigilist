import { useEffect, useState } from 'react';
import { getPipeline } from '../api/client';
import { AI_RUN_STARTED_EVENT, aiActivity } from '../utils/aiActivity';
import type { PipelineStatus } from '../types';

const ACTIVE_POLL_MS = 15_000;
const IDLE_POLL_MS = 60_000;

/**
 * Matter-wide pill shown on every screen while any AI pipeline stage is
 * live ("AI analysis running · Summaries (waiting on batch)"), or muted
 * when the latest run left a stage failed. Hidden when everything is done.
 */
export default function AiActivityIndicator({ productionId }: { productionId: number }) {
  const [status, setStatus] = useState<PipelineStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    // Each poll takes a ticket; only the latest one schedules the next tick,
    // so an early re-poll (run started) never leaves two loops running.
    let ticket = 0;

    const poll = async () => {
      window.clearTimeout(timer);
      const mine = ++ticket;
      let next = IDLE_POLL_MS;
      try {
        const info = await getPipeline(productionId);
        if (cancelled || mine !== ticket) return;
        setStatus(info.status);
        if (aiActivity(info.status)?.kind === 'running') next = ACTIVE_POLL_MS;
      } catch {
        // Transient: try again on the idle cadence.
      }
      // A hidden tab stops here; visibilitychange restarts the loop.
      if (!cancelled && mine === ticket && document.visibilityState !== 'hidden') {
        timer = window.setTimeout(poll, next);
      }
    };

    const onRunStarted = () => { void poll(); };
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        window.clearTimeout(timer);
        ticket += 1; // drop any in-flight poll's reschedule
      } else {
        void poll();
      }
    };
    window.addEventListener(AI_RUN_STARTED_EVENT, onRunStarted);
    document.addEventListener('visibilitychange', onVisibility);
    if (document.visibilityState !== 'hidden') void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.removeEventListener(AI_RUN_STARTED_EVENT, onRunStarted);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [productionId]);

  const activity = aiActivity(status);
  if (!activity) return null;

  const running = activity.kind === 'running';
  return (
    <div className={`ai-activity-pill${running ? '' : ' is-failed'}`} role="status" aria-live="polite">
      <span className="brief-ai-mark" aria-hidden="true">✦</span>
      {running
        ? <>AI analysis running · {activity.stage} ({activity.state})</>
        : <>AI analysis: {activity.stage} failed</>}
    </div>
  );
}
