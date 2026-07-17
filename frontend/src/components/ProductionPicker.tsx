import { useState } from 'react';
import AppHeader from './AppHeader';
import { deleteProduction } from '../api/client';
import type { ProductionInfo } from '../types';

interface Props {
  productions: ProductionInfo[];
  onSelect: (production: ProductionInfo) => void;
  onIngest: () => void;
  onDeleted?: () => void;
}

const dateFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });

export default function ProductionPicker({ productions, onSelect, onIngest, onDeleted }: Props) {
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const handleDelete = async (p: ProductionInfo) => {
    setDeletingId(p.id);
    setDeleteError(null);
    try {
      await deleteProduction(p.id);
      onDeleted?.();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setDeletingId(null);
      setConfirmId(null);
    }
  };

  return (
    <div className="case-desk">
      <AppHeader productions={productions} onOpenIngest={onIngest} />

      <div className="content-area case-desk-content">
        <h1 className="case-desk-title">Your productions</h1>
        <p className="case-desk-sub">Pick a production to continue its review.</p>
        {deleteError && <p className="case-desk-error" role="alert">{deleteError}</p>}

        <div className="case-desk-grid">
          {productions.map(p => (
            <div key={p.id} className="case-card-wrap">
              <button type="button" className="case-card card" onClick={() => onSelect(p)}>
                <div className="case-card-name">{p.name}</div>
                {p.description && <div className="case-card-desc">{p.description}</div>}
                {/* Phase 2 slot: one-line AI theme summary from the production brief. */}
                <div className="case-card-meta">
                  <span>{p.document_count.toLocaleString()} document{p.document_count === 1 ? '' : 's'}</span>
                  <span className="case-card-dot">·</span>
                  <span>added {dateFmt.format(new Date(p.created_at))}</span>
                </div>
                <div className="case-card-badges">
                  {p.is_owner
                    ? <span className="badge badge-blue">Owner</span>
                    : <span className="badge badge-gray">Shared</span>}
                </div>
              </button>

              {p.is_owner && (
                <button
                  type="button"
                  className="case-card-delete"
                  onClick={() => setConfirmId(p.id)}
                  title="Delete production"
                  aria-label={`Delete production ${p.name}`}
                >
                  ×
                </button>
              )}

              {confirmId === p.id && (
                <div className="case-card-confirm">
                  <div className="case-card-confirm-title">Delete "{p.name}"?</div>
                  <div className="case-card-confirm-body">
                    This permanently removes all documents, tags, notes, and uploaded files. Cannot be undone.
                  </div>
                  <div className="case-card-confirm-actions">
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

          <button type="button" className="case-card case-card-new card" onClick={onIngest}>
            <div className="case-card-new-plus">＋</div>
            <div className="case-card-new-label">Ingest a production</div>
            <div className="case-card-desc">Load a new document production into Vigilist.</div>
          </button>
        </div>
      </div>
    </div>
  );
}
