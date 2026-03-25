import { useAuth } from '../hooks/useAuth';
import type { ProductionInfo } from '../types';

interface Props {
  productions: ProductionInfo[];
  onSelect: (production: ProductionInfo) => void;
  onIngest: () => void;
}

export default function ProductionPicker({ productions, onSelect, onIngest }: Props) {
  const { user, logout } = useAuth();

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
            <div key={p.id} className="production-card card" onClick={() => onSelect(p)}>
              <div className="production-card-name">{p.name}</div>
              {p.description && <div className="production-card-desc">{p.description}</div>}
              <div className="production-card-meta">
                {p.is_owner ? (
                  <span className="badge badge-blue">Owner</span>
                ) : (
                  <span className="badge badge-gray">Shared</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
