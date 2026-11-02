import { useEffect, useState } from 'react';
import { decideRedactionQc, getRedactionQc } from '../api/client';
import { REDACTION_REASON_LABELS } from '../types';
import type { Redaction, RedactionQc } from '../types';
import { showToast } from './Toast';

interface Props {
  docId: string;
  redactions: Redaction[];
  /** Changes whenever a redaction is saved, so QC status refreshes. */
  version: number;
  selectedId: number | null;
  onSelect: (r: Redaction) => void;
}

const STATUS_LABELS: Record<string, string> = {
  not_applicable: 'No redactions',
  pending: 'QC pending',
  approved: 'QC approved',
  rejected: 'QC rejected',
};

/** The document's redactions by page, and the manager's QC sign-off. */
export default function RedactionsPanel({ docId, redactions, version, selectedId, onSelect }: Props) {
  const [qc, setQc] = useState<RedactionQc | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    getRedactionQc(docId).then(q => { if (live) setQc(q); })
      .catch(e => console.warn('getRedactionQc failed:', e));
    return () => { live = false; };
  }, [docId, version]);

  const decide = async (decision: 'approved' | 'rejected') => {
    setBusy(true);
    try {
      await decideRedactionQc(docId, decision, note);
      setNote('');
      setQc(await getRedactionQc(docId));
    } catch (e) {
      // Reviewers get 403: only managers sign off.
      showToast(`QC not recorded: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
    } finally {
      setBusy(false);
    }
  };

  const sorted = [...redactions].sort((a, b) => a.page_num - b.page_num || a.y_pct - b.y_pct);

  return (
    <div style={{ padding: 'var(--space-3)', overflow: 'auto', flex: 1, fontSize: 'var(--text-sm)' }}>
      {qc && (
        <div style={{ marginBottom: 'var(--space-3)' }}>
          <span className={`badge ${qc.qc_status === 'approved' ? 'badge-green' : qc.qc_status === 'rejected' ? 'badge-red' : 'badge-yellow'}`}>
            {STATUS_LABELS[qc.qc_status] ?? qc.qc_status}
          </span>
          {qc.latest_decision?.note && (
            <div style={{ marginTop: 4, color: 'var(--color-neutral-500)' }}>“{qc.latest_decision.note}”</div>
          )}
          {redactions.length > 0 && (
            <div style={{ display: 'flex', gap: 6, marginTop: 'var(--space-2)', flexWrap: 'wrap' }}>
              <input className="input input-sm" style={{ flex: 1, minWidth: 120 }} placeholder="QC note (optional)"
                     aria-label="QC note" value={note} onChange={e => setNote(e.target.value)} />
              <button type="button" className="btn btn-primary btn-xs" disabled={busy} onClick={() => decide('approved')}>Approve</button>
              <button type="button" className="btn btn-secondary btn-xs" disabled={busy} onClick={() => decide('rejected')}>Reject</button>
            </div>
          )}
        </div>
      )}
      {sorted.length === 0 ? (
        <div className="empty-state" style={{ padding: 'var(--space-3)' }}>
          No redactions yet. Turn on Redact (Shift+R) and drag over the page.
        </div>
      ) : (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {sorted.map(r => (
            <li key={r.id}>
              <button type="button" onClick={() => onSelect(r)}
                      style={{ width: '100%', textAlign: 'left', padding: '6px 8px', borderRadius: 4, cursor: 'pointer',
                               border: `1px solid ${r.id === selectedId ? '#f6ad55' : 'rgba(44,62,107,0.12)'}`,
                               background: r.id === selectedId ? 'rgba(246,173,85,0.1)' : 'transparent', font: 'inherit' }}>
                <div><b>Page {r.page_num}</b> · {REDACTION_REASON_LABELS[r.reason_code] ?? r.reason_code}</div>
                <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>
                  {r.id < 0 ? 'Saving…' : new Date(r.updated_at ?? r.created_at).toLocaleString()}
                  {r.note ? ` · ${r.note}` : ''}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
