import { useState } from 'react';
import { deleteProduction } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import type { ProductionInfo } from '../types';

interface Props {
  productions: ProductionInfo[];
  onSelect: (production: ProductionInfo) => void;
  onIngest: () => void;
  onDeleted?: () => void;
}

export default function ProductionPicker({ productions, onSelect, onIngest, onDeleted }: Props) {
  const { user, logout } = useAuth();
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);

  const handleDelete = async (p: ProductionInfo) => {
    setDeletingId(p.id);
    try {
      await deleteProduction(p.id);
      onDeleted?.();
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setDeletingId(null);
      setConfirmId(null);
    }
  };

  return (
    <div style={{ minHeight: '100vh', background: 'var(--color-neutral-50)' }}>
      <div className="app-header">
        <span className="logo">Vigilist</span>
        <div className="user-menu">
          <button className="btn-header" onClick={onIngest}>+ Ingest</button>
          <span style={{ opacity: 0.7 }}>{user?.displayName || user?.email}</span>
          <button className="btn-header" onClick={logout}>Sign out</button>
        </div>
      </div>

      <div className="content-area" style={{ paddingTop: 'var(--space-8)' }}>
        <h2 className="section-title" style={{ marginBottom: 'var(--space-6)', textAlign: 'center' }}>
          Select a Production
        </h2>

        <div className="production-grid">
          {productions.map(p => (
            <div key={p.id} style={{ position: 'relative' }}>
              <button
                type="button"
                className="production-card card"
                onClick={() => onSelect(p)}
              >
                <div className="production-card-name">{p.name}</div>
                {p.description && <div className="production-card-desc">{p.description}</div>}
                <div className="production-card-meta">
                  {p.is_owner ? (
                    <span className="badge badge-blue">Owner</span>
                  ) : (
                    <span className="badge badge-gray">Shared</span>
                  )}
                </div>
              </button>

              {p.is_owner && (
                <button
                  type="button"
                  onClick={() => setConfirmId(p.id)}
                  title="Delete production"
                  aria-label="Delete production"
                  style={{
                    position: 'absolute',
                    top: 8,
                    right: 8,
                    background: 'transparent',
                    border: 'none',
                    color: 'rgba(44,62,107,0.35)',
                    cursor: 'pointer',
                    fontSize: 16,
                    padding: '4px 8px',
                    borderRadius: 4,
                    lineHeight: 1,
                    zIndex: 1,
                  }}
                >
                  ×
                </button>
              )}

              {confirmId === p.id && (
                <div
                  style={{
                    position: 'absolute',
                    inset: 0,
                    background: 'rgba(255,255,255,0.96)',
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    borderRadius: 'var(--radius-lg)',
                    padding: 'var(--space-4)',
                    gap: 'var(--space-3)',
                    zIndex: 2,
                  }}
                >
                  <div style={{ fontSize: 'var(--text-sm)', fontWeight: 600, color: 'var(--color-ink)', textAlign: 'center' }}>
                    Delete "{p.name}"?
                  </div>
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', textAlign: 'center' }}>
                    This permanently removes all documents, tags, notes, and uploaded files. Cannot be undone.
                  </div>
                  <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => setConfirmId(null)}
                      disabled={deletingId === p.id}
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger btn-sm"
                      onClick={() => handleDelete(p)}
                      disabled={deletingId === p.id}
                    >
                      {deletingId === p.id ? 'Deleting…' : 'Delete'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
