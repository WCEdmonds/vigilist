import { useCallback, useEffect, useState } from 'react';
import { exportPrivilegeLogCsv, getPrivilegeLog, updatePrivilege } from '../api/client';
import type { PrivilegeDisposition, PrivilegeLogRow } from '../types';

interface Props {
  productionId: number;
  onClose: () => void;
  onOpenDocument: (docId: string) => void;
}

const DISPOSITION_LABELS: Record<PrivilegeDisposition, string> = {
  withhold: 'Withhold',
  redact_in_part: 'Redact in part',
  produce: 'Produce (remove from log)',
};

const QC_LABELS: Record<string, string> = {
  not_applicable: '—',
  pending: 'Pending',
  approved: 'Approved',
  rejected: 'Rejected',
};

/**
 * The production's privilege log: every withheld or redacted document, with
 * the entry opposing counsel will read. Disposition and description are
 * derived from privilege tags and redactions until a manager writes their
 * own; clearing the text goes back to the generated sentence.
 */
export default function PrivilegeLog({ productionId, onClose, onOpenDocument }: Props) {
  const [rows, setRows] = useState<PrivilegeLogRow[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    getPrivilegeLog(productionId)
      .then(r => { setRows(r); setDrafts({}); })
      .catch(e => setError(e instanceof Error ? e.message : 'Could not load the privilege log'));
  }, [productionId]);

  useEffect(() => { load(); }, [load]);

  // Esc closes, matching the other modals.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const save = async (row: PrivilegeLogRow, body: Parameters<typeof updatePrivilege>[1]) => {
    setSaving(row.document_id);
    setError(null);
    try {
      await updatePrivilege(row.document_id, body);
      load();
    } catch (e) {
      setError(`${row.bates_begin}: ${e instanceof Error ? e.message : 'save failed'}`);
    } finally {
      setSaving(null);
    }
  };

  const saveDescription = (row: PrivilegeLogRow) => {
    const draft = drafts[row.document_id];
    if (draft === undefined) return;
    const next = draft.trim() || null;
    if (next === row.description_override) return;
    save(row, { description: next });
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel modal-large" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2 className="modal-title">Privilege Log</h2>
          <button className="modal-close-btn" aria-label="Close" onClick={onClose}>&times;</button>
        </div>

        <div className="modal-body">
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
            <span style={{ color: 'var(--color-neutral-500)', fontSize: 'var(--text-sm)' }}>
              {rows === null ? 'Loading…' : `${rows.length.toLocaleString()} ${rows.length === 1 ? 'entry' : 'entries'}`}
              {' · '}documents with a privilege tag, redactions, or a disposition you set
            </span>
            <button className="btn btn-secondary" style={{ marginLeft: 'auto' }}
                    onClick={() => exportPrivilegeLogCsv(productionId).catch(e => setError(e.message))}>
              Export CSV
            </button>
          </div>

          {error && <div role="alert" style={{ color: 'var(--color-danger, #b42318)', fontSize: 'var(--text-sm)', marginBottom: '1rem' }}>{error}</div>}

          <table className="doc-table">
            <thead>
              <tr>
                <th>Bates</th>
                <th>Date</th>
                <th>Author / Recipients</th>
                <th>Disposition</th>
                <th>Basis</th>
                <th style={{ minWidth: 320 }}>Log description</th>
                <th>Redaction QC</th>
              </tr>
            </thead>
            <tbody>
              {(rows ?? []).map(row => {
                const busy = saving === row.document_id;
                return (
                  <tr key={row.document_id} style={busy ? { opacity: 0.6 } : undefined}>
                    <td>
                      <button type="button" className="btn btn-ghost btn-xs"
                              onClick={() => onOpenDocument(row.document_id)}>
                        {row.bates_begin}
                      </button>
                      {row.bates_end !== row.bates_begin && (
                        <div style={{ fontSize: '0.75em', color: 'var(--color-neutral-500)' }}>– {row.bates_end}</div>
                      )}
                    </td>
                    <td>{row.doc_date ?? ''}</td>
                    <td style={{ fontSize: '0.8em', maxWidth: 220, whiteSpace: 'normal' }}>
                      {row.author && <div>{row.author}</div>}
                      {row.recipients && <div style={{ color: 'var(--color-neutral-500)' }}>→ {row.recipients}</div>}
                    </td>
                    <td>
                      <select
                        aria-label={`Disposition for ${row.bates_begin}`}
                        value={row.disposition_override ?? ''}
                        disabled={busy}
                        onChange={e => save(row, {
                          disposition: (e.target.value || null) as PrivilegeDisposition | null,
                        })}
                      >
                        {/* With an override set, the effective value is the override, not what tags would derive. */}
                        <option value="">{row.disposition_override ? 'Automatic' : `Automatic (${DISPOSITION_LABELS[row.disposition]})`}</option>
                        {(Object.keys(DISPOSITION_LABELS) as PrivilegeDisposition[]).map(d => (
                          <option key={d} value={d}>{DISPOSITION_LABELS[d]}</option>
                        ))}
                      </select>
                    </td>
                    <td style={{ fontSize: '0.8em' }}>{row.basis.join(', ')}</td>
                    <td>
                      <textarea
                        aria-label={`Log description for ${row.bates_begin}`}
                        rows={2}
                        style={{ width: '100%', fontSize: '0.85em', resize: 'vertical' }}
                        placeholder={row.generated_description}
                        value={drafts[row.document_id] ?? row.description_override ?? ''}
                        disabled={busy}
                        onChange={e => setDrafts(d => ({ ...d, [row.document_id]: e.target.value }))}
                        onBlur={() => saveDescription(row)}
                      />
                    </td>
                    <td>{QC_LABELS[row.qc_status] ?? row.qc_status}</td>
                  </tr>
                );
              })}
              {rows !== null && rows.length === 0 && (
                <tr><td colSpan={7} style={{ textAlign: 'center' }}>
                  Nothing on the log yet — tag documents with a privilege tag or redact them.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
