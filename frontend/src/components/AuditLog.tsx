import { useEffect, useState } from 'react';
import { getAuditLogs } from '../api/client';
import { auth } from '../firebase';
import type { AuditLogEntry } from '../types';

interface Props {
  productionId: number;
  onClose: () => void;
}

/** The slice of AI-review audit `details` the prose renderer reads. */
interface ReviewSnapshot {
  description?: string;
  event_date?: string | null;
  date_precision?: string;
  event_type?: string;
  significance?: number | null;
}

interface ReviewDetails {
  actor?: string;
  reason?: string;
  confidence?: number;
  snapshot?: ReviewSnapshot;
  before?: ReviewSnapshot;
  after?: ReviewSnapshot;
  absorbed?: number[];
  merged_into?: number;
  merged?: number;
  deleted?: number;
  edited?: number;
  rerated?: number;
  skipped?: number;
  event_count?: number;
  model?: string;
}

const pct = (c?: number) => (c == null ? '' : ` · ${Math.round(c * 100)}% confident`);

/** Field-level before → after description for review edits/rerates. */
function diffLine(before?: ReviewSnapshot, after?: ReviewSnapshot): string {
  if (!before || !after) return '';
  const parts: string[] = [];
  if (before.event_date !== after.event_date || before.date_precision !== after.date_precision) {
    parts.push(`date ${before.event_date ?? 'unset'} → ${after.event_date ?? 'unset'}`);
  }
  if (before.event_type !== after.event_type) parts.push(`type ${before.event_type} → ${after.event_type}`);
  if (before.description !== after.description) parts.push('description rewritten');
  if (before.significance !== after.significance) {
    parts.push(`significance ${before.significance ?? '—'} → ${after.significance ?? '—'}`);
  }
  return parts.join(', ');
}

/** Prose for the AI-review rows; null falls back to the raw-JSON cell. */
function describeReview(action: string, d: ReviewDetails): string | null {
  switch (action) {
    case 'event_deleted_by_review': {
      const what = d.snapshot?.description ?? 'event';
      const when = d.snapshot?.event_date ?? 'undated';
      const via = d.merged_into != null ? ` (duplicate of event ${d.merged_into})` : '';
      return `Removed “${what}” (${when})${via} — ${d.reason ?? ''}${pct(d.confidence)}`;
    }
    case 'event_merged_by_review':
      return `Absorbed ${d.absorbed?.length ?? 0} duplicate event(s) — ${d.reason ?? ''}${pct(d.confidence)}`;
    case 'event_edited_by_review':
      return `Corrected ${diffLine(d.before, d.after) || 'fields'} — ${d.reason ?? ''}${pct(d.confidence)}`;
    case 'event_rerated_by_review':
      return `Significance ${d.before?.significance ?? '—'} → ${d.after?.significance ?? '—'} — ${d.reason ?? ''}${pct(d.confidence)}`;
    case 'timeline_review_completed': {
      const rerated = d.rerated != null ? `, ${d.rerated} re-rated` : '';
      return `Run finished: ${d.merged ?? 0} merged, ${d.deleted ?? 0} removed, `
        + `${d.edited ?? 0} corrected${rerated}, ${d.skipped ?? 0} skipped `
        + `(${d.event_count ?? '?'} events reviewed · ${d.model ?? 'AI'})`;
    }
    case 'timeline_review_triggered':
      return 'AI review requested';
    default:
      return null;
  }
}

export default function AuditLog({ productionId, onClose }: Props) {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [actionFilter, setActionFilter] = useState('');
  const perPage = 50;

  useEffect(() => {
    getAuditLogs(page, perPage, productionId, undefined, actionFilter || undefined)
      .then(res => { setLogs(res.logs); setTotal(res.total); });
  }, [page, productionId, actionFilter]);

  // Esc closes, matching the other modals.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const totalPages = Math.ceil(total / perPage);

  const handleExportCsv = async () => {
    const token = await auth.currentUser?.getIdToken();
    const params = new URLSearchParams();
    params.set('production_id', String(productionId));
    if (actionFilter) params.set('action', actionFilter);
    const res = await fetch(`/api/audit/export/csv?${params}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'audit_log.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel modal-large" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2 className="modal-title">Audit Log</h2>
          <button className="modal-close-btn" aria-label="Close" onClick={onClose}>&times;</button>
        </div>

        <div className="modal-body">
        <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem' }}>
          <select value={actionFilter} onChange={e => { setActionFilter(e.target.value); setPage(1); }}>
            <option value="">All actions</option>
            <option value="tag_applied">Tag Applied</option>
            <option value="tag_removed">Tag Removed</option>
            <option value="bulk_tag_applied">Bulk Tag</option>
            <option value="note_created">Note Created</option>
            <option value="note_updated">Note Updated</option>
            <option value="note_deleted">Note Deleted</option>
            <option value="document_viewed">Document Viewed</option>
            <option value="search_executed">Search Executed</option>
            <option value="user_login">Login</option>
            <option value="user_invited">User Invited</option>
            <option value="access_revoked">Access Revoked</option>
            <option value="ai_chat_started">AI Chat Started</option>
            <option value="similar_docs_requested">Find Similar</option>
            <option value="brief_generated">Brief Generated</option>
            <option value="summary_batch_completed">Summaries Completed</option>
            <option value="classification_run">Classification Run</option>
            <option value="ai_suggestion_accepted">AI Suggestion Accepted</option>
            <option value="ai_suggestion_overridden">AI Suggestion Overridden</option>
            <option value="ai_suggestions_bulk_accepted">Bulk Accept</option>
            <option value="pipeline_run_requested">Pipeline Run</option>
            <option value="timeline_review_completed">AI Review Runs</option>
            <option value="event_merged_by_review">AI Review: Merges</option>
            <option value="event_deleted_by_review">AI Review: Removals</option>
            <option value="event_edited_by_review">AI Review: Corrections</option>
            <option value="event_rerated_by_review">AI Review: Re-ratings</option>
          </select>
          <button className="btn btn-secondary" onClick={handleExportCsv}>
            Export CSV
          </button>
        </div>

        <table className="doc-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>User</th>
              <th>Action</th>
              <th>Resource</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {logs.map(log => {
              const details = log.details as ReviewDetails;
              const isAi = details?.actor === 'ai_timeline_review';
              const prose = describeReview(log.action, details ?? {});
              return (
                <tr key={log.id}>
                  <td>{new Date(log.created_at).toLocaleString()}</td>
                  {/* Review actions are attributed to the owner for accountability,
                      but the reader should see at a glance the AI made the change. */}
                  <td title={isAi ? `on behalf of ${log.user_email}` : undefined}>
                    {isAi ? 'AI review' : log.user_email}
                  </td>
                  <td>{log.action.replace(/_/g, ' ')}</td>
                  <td>{log.resource_type}{log.resource_id ? `: ${log.resource_id.slice(0, 8)}...` : ''}</td>
                  {prose !== null ? (
                    <td style={{ fontSize: '0.8em', maxWidth: 380, whiteSpace: 'normal' }}
                        title={JSON.stringify(log.details)}>
                      {prose}
                    </td>
                  ) : (
                    <td style={{ fontSize: '0.8em', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {JSON.stringify(log.details)}
                    </td>
                  )}
                </tr>
              );
            })}
            {logs.length === 0 && (
              <tr><td colSpan={5} style={{ textAlign: 'center' }}>No audit logs found</td></tr>
            )}
          </tbody>
        </table>

        {totalPages > 1 && (
          <div className="pagination">
            <button disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
            <span>Page {page} of {totalPages}</span>
            <button disabled={page >= totalPages} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        )}
        </div>
      </div>
    </div>
  );
}
