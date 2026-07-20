import { useCallback, useEffect, useState } from 'react';
import { getProductionAccess, getProductionInvites, inviteUser, revokeAccess } from '../api/client';
import type { ProductionAccessEntry, PendingInviteEntry } from '../types';
import { showToast } from './Toast';

interface Props {
  productionId: number;
  onClose: () => void;
}

export default function ManageAccess({ productionId, onClose }: Props) {
  const [access, setAccess] = useState<ProductionAccessEntry[]>([]);
  const [invites, setInvites] = useState<PendingInviteEntry[]>([]);
  const [email, setEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('reviewer');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [a, i] = await Promise.all([
        getProductionAccess(productionId),
        getProductionInvites(productionId),
      ]);
      setAccess(a);
      setInvites(i);
    } catch (e) {
      showToast(`Could not load access list: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
    }
  }, [productionId]);

  useEffect(() => { load(); }, [load]);

  const handleInvite = async () => {
    if (!email.trim()) return;
    setError('');
    setMessage('');
    setLoading(true);
    try {
      const res = await inviteUser(productionId, email.trim(), inviteRole);
      setMessage(res.status === 'granted' ? `Access granted to ${res.email}` : `Invitation sent to ${res.email}`);
      setEmail('');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to invite');
    } finally {
      setLoading(false);
    }
  };

  const handleRevoke = async (userId: string, displayName: string) => {
    if (revokingId) return;
    if (!window.confirm(`Revoke access for ${displayName}? They will immediately lose access to this production.`)) return;
    setError('');
    setRevokingId(userId);
    try {
      await revokeAccess(productionId, userId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to revoke');
    } finally {
      setRevokingId(null);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3 className="modal-title">Manage Access</h3>
          <button className="modal-close-btn" aria-label="Close" onClick={onClose}>&times;</button>
        </div>

        {/* Invite form */}
        <div style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--color-neutral-200)' }}>
          <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
            <label htmlFor="invite-email" className="visually-hidden">Email address</label>
            <input
              id="invite-email"
              type="email"
              className="input input-sm"
              placeholder="Email address"
              value={email}
              onChange={e => setEmail(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleInvite()}
              style={{ flex: 1 }}
              disabled={loading}
            />
            <label htmlFor="invite-role" className="visually-hidden">Role</label>
            <select
              id="invite-role"
              className="input input-sm"
              value={inviteRole}
              onChange={e => setInviteRole(e.target.value)}
              disabled={loading}
            >
              <option value="reviewer">Reviewer</option>
              <option value="readonly">Read Only</option>
              <option value="manager">Manager</option>
              <option value="admin">Admin</option>
            </select>
            <button className="btn btn-primary btn-sm" onClick={handleInvite} disabled={loading || !email.trim()}>
              {loading ? 'Sending…' : 'Invite'}
            </button>
          </div>
          {message && <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-success-700)' }}>{message}</div>}
          {error && <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-danger-700)' }}>{error}</div>}
        </div>

        {/* Users with access */}
        <div style={{ padding: 'var(--space-3) var(--space-4)', maxHeight: 400, overflowY: 'auto' }}>
          {access.length === 0 && invites.length === 0 && (
            <div style={{ color: 'var(--color-neutral-400)', fontSize: 'var(--text-sm)', padding: 'var(--space-4)', textAlign: 'center' }}>
              No one else has access yet
            </div>
          )}
          {access.map(a => (
            <div key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: 'var(--space-2) 0', borderBottom: '1px solid var(--color-neutral-100)' }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 'var(--text-sm)', fontWeight: 'var(--font-medium)' }}>{a.user_display_name || a.user_email}</div>
                {a.user_display_name && <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)' }}>{a.user_email}</div>}
              </div>
              <span className="badge badge-blue" style={{ textTransform: 'capitalize' }}>{a.role}</span>
              <button
                className="btn btn-ghost btn-xs"
                style={{ color: 'var(--color-danger-600)' }}
                onClick={() => handleRevoke(a.user_id, a.user_display_name || a.user_email)}
                disabled={revokingId === a.user_id}
              >
                {revokingId === a.user_id ? 'Removing…' : 'Remove'}
              </button>
            </div>
          ))}
          {invites.map(inv => (
            <div key={inv.id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: 'var(--space-2) 0', borderBottom: '1px solid var(--color-neutral-100)' }}>
              <div style={{ flex: 1, fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)' }}>{inv.email}</div>
              <span className="badge badge-yellow">Pending</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
