import { REDACTION_REASON_LABELS } from '../types';
import type { RedactionReason } from '../types';

interface Props {
  active: boolean;
  onToggle: () => void;
  reason: RedactionReason;
  onReason: (r: RedactionReason) => void;
  onRedactPage: () => void;
  preview: boolean;
  onPreview: (on: boolean) => void;
  /** Set when drawing isn't possible right now (e.g. the page is rotated). */
  blockedBy?: string | null;
}

const REASONS = Object.keys(REDACTION_REASON_LABELS) as RedactionReason[];

export default function RedactionToolbar({ active, onToggle, reason, onReason, onRedactPage, preview, onPreview, blockedBy }: Props) {
  return (
    <div className="redaction-toolbar" style={{
      display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap',
      padding: '6px var(--space-3)', borderBottom: '1px solid rgba(44,62,107,0.1)', fontSize: 'var(--text-sm)',
      background: active ? 'rgba(246,173,85,0.12)' : undefined,
    }}>
      <button type="button" className={active ? 'btn btn-primary btn-xs' : 'btn btn-secondary btn-xs'}
              aria-pressed={active} onClick={onToggle} title="Redact mode (R) — Esc to leave">
        {active ? 'Redacting' : 'Redact'}
      </button>
      {active && (
        <>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ color: 'var(--color-neutral-500)' }}>Reason</span>
            <select className="input input-sm" value={reason}
                    onChange={e => onReason(e.target.value as RedactionReason)}>
              {REASONS.map(r => <option key={r} value={r}>{REDACTION_REASON_LABELS[r]}</option>)}
            </select>
          </label>
          <button type="button" className="btn btn-ghost btn-xs" onClick={onRedactPage} disabled={!!blockedBy}
                  title="Black out the whole page nearest the middle of the screen">
            Redact page
          </button>
          <span style={{ color: 'var(--color-neutral-500)' }}>
            {blockedBy ?? 'Drag to draw · click a box to select · Delete removes · arrows nudge'}
          </span>
        </>
      )}
      <label style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}>
        <input type="checkbox" checked={preview} onChange={e => onPreview(e.target.checked)} />
        Preview as produced
      </label>
    </div>
  );
}
