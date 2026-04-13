import { useEffect, useState } from 'react';
import { getAuditLogs } from '../api/client';
import { auth } from '../firebase';
import type { AuditLogEntry } from '../types';

interface Props {
  productionId: number;
  onClose: () => void;
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
          <h2 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>Audit Log</h2>
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
            {logs.map(log => (
              <tr key={log.id}>
                <td>{new Date(log.created_at).toLocaleString()}</td>
                <td>{log.user_email}</td>
                <td>{log.action.replace(/_/g, ' ')}</td>
                <td>{log.resource_type}{log.resource_id ? `: ${log.resource_id.slice(0, 8)}...` : ''}</td>
                <td style={{ fontSize: '0.8em', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {JSON.stringify(log.details)}
                </td>
              </tr>
            ))}
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
