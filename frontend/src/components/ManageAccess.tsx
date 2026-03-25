import { useEffect, useState } from 'react';
import { getProductionAccess, getProductionInvites, inviteUser, revokeAccess } from '../api/client';
import type { ProductionAccessEntry, PendingInviteEntry } from '../types';

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

  const load = async () => {
    const [a, i] = await Promise.all([
      getProductionAccess(productionId),
      getProductionInvites(productionId),
    ]);
    setAccess(a);
    setInvites(i);
  };

  useEffect(() => { load(); }, [productionId]);

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
    } catch (e: any) {
      setError(e.message || 'Failed to invite');
    } finally {
      setLoading(false);
    }
  };

  const handleRevoke = async (userId: string) => {
    try {
      await revokeAccess(productionId, userId);
      await load();
    } catch (e: any) {
      setError(e.message || 'Failed to revoke');
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>Manage Access</h3>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
        </div>

        {/* Invite form */}
        <div style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--color-neutral-200)' }}>
          <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
            <input
              type="email"
              className="input input-sm"
              placeholder="Email address"
              value={email}
              onChange={e => setEmail(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleInvite()}
              style={{ flex: 1 }}
            />
            <select
              className="input input-sm"
              value={inviteRole}
              onChange={e => setInviteRole(e.target.value)}
            >
              <option value="reviewer">Reviewer</option>
              <option value="readonly">Read Only</option>
              <option value="manager">Manager</option>
              <option value="admin">Admin</option>
            </select>
            <button className="btn btn-primary btn-sm" onClick={handleInvite} disabled={loading}>
              {loading ? 'Sending...' : 'Invite'}
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
              <button className="btn btn-ghost btn-xs" style={{ color: 'var(--color-danger-600)' }} onClick={() => handleRevoke(a.user_id)}>
                Remove
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
