import { useMemo, useState } from 'react';
import { applyContainerSelection } from '../api/client';
import type { EstimateModel, IngestContainer, IngestJob } from '../types';

const TYPE_LABELS: Record<string, string> = {
  email: 'Email',
  calendar: 'Calendar',
  contact: 'Contacts',
  task: 'Tasks',
  note: 'Notes',
  other: 'Other items',
};
const TYPE_ORDER = ['email', 'calendar', 'contact', 'task', 'note', 'other'];
const DEFAULT_ON = new Set(['email', 'calendar']);

const fmtBytes = (b: number) =>
  b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(0, Math.round(b / 1e3))} KB`;

const fmtMinutes = (m: number) => {
  if (m < 1) return 'under a minute';
  if (m < 90) return `about ${Math.round(m)} min`;
  return `about ${(m / 60).toFixed(1)} hours`;
};

interface Estimate { items: number; bytes: number; usd: number; minutes: number; storageGb: number }

/** Same linear model as the backend's estimate_load, fed by the API's constants. */
function liveEstimate(
  c: IngestContainer, types: Set<string>, includeRecoverable: boolean, model: EstimateModel,
): Estimate {
  let items = 0;
  let bytes = 0;
  for (const t of c.types) {
    if (types.has(t.type)) { items += t.count; bytes += t.bytes; }
  }
  if (includeRecoverable) {
    for (const t of c.recoverable.types) {
      if (types.has(t.type)) { items += t.count; bytes += t.bytes; }
    }
  }
  return {
    items,
    bytes,
    usd: Math.round((items * model.usd_per_item + bytes * model.usd_per_byte) * 100) / 100,
    minutes: Math.round((items / model.items_per_second / 60) * 10) / 10,
    storageGb: Math.round((bytes / 1e9) * 100) / 100,
  };
}

function ContainerCard({ jobId, container, model, onApplied }: {
  jobId: string;
  container: IngestContainer;
  model: EstimateModel;
  onApplied: () => void;
}) {
  const present = useMemo(() => {
    const seen = new Set<string>();
    for (const t of container.types) seen.add(t.type);
    for (const t of container.recoverable.types) seen.add(t.type);
    return TYPE_ORDER.filter(t => seen.has(t));
  }, [container]);
  const [types, setTypes] = useState<Set<string>>(() => new Set(present.filter(t => DEFAULT_ON.has(t))));
  const [includeRecoverable, setIncludeRecoverable] = useState(container.recoverable.count > 0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const est = liveEstimate(container, types, includeRecoverable, model);
  const overBudget = est.usd > model.budget_usd;

  const toggle = (t: string) => setTypes(prev => {
    const next = new Set(prev);
    if (next.has(t)) next.delete(t); else next.add(t);
    return next;
  });

  const confirm = async () => {
    setSubmitting(true);
    setError('');
    try {
      await applyContainerSelection(jobId, container.storage_path, [...types], includeRecoverable);
      onApplied();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start the load');
      setSubmitting(false);
    }
  };

  const byType = (t: string) => container.types.find(x => x.type === t);
  const rowStyle: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: '6px 0',
    fontSize: 'var(--text-sm)', cursor: 'pointer',
  };

  return (
    <div style={{ border: '1px solid var(--color-neutral-200)', borderRadius: 'var(--radius-md)', padding: 'var(--space-3)', textAlign: 'left' }}>
      <div style={{ fontWeight: 'var(--font-semibold)', fontSize: 'var(--text-sm)' }}>{container.name}</div>
      <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-2)' }}>
        {fmtBytes(container.size_bytes)} of items found. Choose what to bring in.
      </div>

      {present.map(t => {
        const row = byType(t);
        const rowRecoverable = container.recoverable.types.find(x => x.type === t);
        const count = (row?.count ?? 0) + (includeRecoverable ? rowRecoverable?.count ?? 0 : 0);
        const bytes = (row?.bytes ?? 0) + (includeRecoverable ? rowRecoverable?.bytes ?? 0 : 0);
        return (
          <label key={t} style={rowStyle}>
            <input type="checkbox" checked={types.has(t)} onChange={() => toggle(t)} />
            <span style={{ flex: 1 }}>{TYPE_LABELS[t] ?? t}</span>
            <span style={{ color: 'var(--color-neutral-500)', fontFamily: 'var(--font-mono)', fontSize: 'var(--text-xs)' }}>
              {count.toLocaleString()} · {fmtBytes(bytes)}
            </span>
          </label>
        );
      })}

      {container.recoverable.count > 0 && (
        <label style={{ ...rowStyle, borderTop: '1px solid var(--color-neutral-100)', marginTop: 4, paddingTop: 10 }}>
          <input type="checkbox" checked={includeRecoverable} onChange={e => setIncludeRecoverable(e.target.checked)} />
          <span style={{ flex: 1 }}>Recoverable / deleted items</span>
          <span style={{ color: 'var(--color-neutral-500)', fontFamily: 'var(--font-mono)', fontSize: 'var(--text-xs)' }}>
            {container.recoverable.count.toLocaleString()} · {fmtBytes(container.recoverable.bytes)}
          </span>
        </label>
      )}

      <div style={{
        marginTop: 'var(--space-3)', padding: 'var(--space-2) var(--space-3)',
        background: 'var(--color-neutral-50)', borderRadius: 'var(--radius-sm)', fontSize: 'var(--text-sm)',
      }}>
        <div>
          <strong>{est.items.toLocaleString()}</strong> items · {fmtMinutes(est.minutes)} · about ${est.usd.toFixed(2)} · {est.storageGb.toFixed(2)} GB stored
        </div>
        {overBudget && (
          <div role="alert" style={{ marginTop: 4, color: 'var(--color-danger-700)', fontWeight: 'var(--font-semibold)' }}>
            This load is estimated to cost more than ${model.budget_usd.toFixed(0)}. Check the selection before continuing.
          </div>
        )}
      </div>

      {error && <div style={{ color: 'var(--color-danger-600)', fontSize: 'var(--text-xs)', marginTop: 'var(--space-2)' }}>{error}</div>}

      <div style={{ marginTop: 'var(--space-3)', display: 'flex', justifyContent: 'flex-end' }}>
        <button className="btn btn-primary" disabled={submitting} onClick={confirm}>
          {submitting ? 'Starting…' : est.items === 0 ? 'Bring in nothing' : 'Confirm and start loading'}
        </button>
      </div>
    </div>
  );
}

interface Props {
  job: IngestJob;
  /** Called after a selection is accepted so the caller refreshes job status. */
  onApplied: () => void;
}

/** "What to bring in": one card per surveyed large container still awaiting a choice. */
export default function ContainerSelection({ job, onApplied }: Props) {
  const model = job.estimate_model as EstimateModel | undefined;
  const pending = (job.containers ?? []).filter(c => !c.selected);
  if (!model || typeof model.usd_per_item !== 'number') return null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
      <div style={{ fontSize: 'var(--text-sm)', fontWeight: 'var(--font-medium)', textAlign: 'center' }}>
        {pending.length === 1 ? 'This mailbox is large.' : 'These mailboxes are large.'} Choose what to bring in before any paid work starts.
      </div>
      {pending.map(c => (
        <ContainerCard key={c.storage_path} jobId={job.id} container={c} model={model} onApplied={onApplied} />
      ))}
    </div>
  );
}
