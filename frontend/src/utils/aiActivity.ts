import type { PipelineStatus } from '../types';

/** Fired on window after any AI run is started, so the activity indicator
 * re-polls at once instead of waiting out its idle interval. */
export const AI_RUN_STARTED_EVENT = 'vigilist:ai-run-started';

export function announceAiRunStarted() {
  window.dispatchEvent(new Event(AI_RUN_STARTED_EVENT));
}

// Display order of the pipeline stages, with their labels.
const STAGES: [keyof PipelineStatus, string][] = [
  ['clustering', 'Clustering'],
  ['summaries', 'Summaries'],
  ['entities', 'Entities'],
  ['timeline_review', 'Timeline review'],
  ['brief', 'Brief'],
];

const ACTIVE_STATE_LABELS: Record<string, string> = {
  running: 'running',
  queued: 'queued',
  waiting: 'waiting on batch',
  applying: 'applying results',
};

// A worker killed mid-stage leaves "running" behind with no further writes.
// Mirrors ProductionBrief / the backend's STALE_RUNNING_MINUTES. Batched
// states (waiting/applying) can legitimately sit for hours, so they never go stale.
const STALE_RUNNING_MINUTES = 45;

export type AiActivity =
  | { kind: 'running'; stage: string; state: string }
  | { kind: 'failed'; stage: string }
  | null;

/** What the matter-wide AI indicator should say for a pipeline status. */
export function aiActivity(status: PipelineStatus | null | undefined, now = Date.now()): AiActivity {
  if (!status) return null;
  const updated = status.updated_at ? new Date(status.updated_at).getTime() : NaN;
  const stale = !Number.isNaN(updated) && now - updated > STALE_RUNNING_MINUTES * 60 * 1000;
  for (const [key, label] of STAGES) {
    const state = String(status[key] ?? '');
    if (!(state in ACTIVE_STATE_LABELS)) continue;
    if (stale && (state === 'running' || state === 'queued')) continue;
    return { kind: 'running', stage: label, state: ACTIVE_STATE_LABELS[state] };
  }
  for (const [key, label] of STAGES) {
    if (status[key] === 'failed') return { kind: 'failed', stage: label };
  }
  return null;
}

/** Cost as a person reads it: fractions of a cent aren't "$0.00". */
export function formatUsd(usd: number): string {
  if (usd > 0 && usd < 0.01) return 'under $0.01';
  return `$${usd.toFixed(2)}`;
}
