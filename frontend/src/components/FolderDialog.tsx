import { useEffect, useState } from 'react';
import { addToFolder, createFolder } from '../api/client';
import type { FolderInfo } from '../types';
import type { BulkTarget } from './BulkTagDialog';

interface Props {
  productionId: number;
  target: BulkTarget;
  /** How many documents the target covers (before attachments). */
  count: number;
  folders: FolderInfo[];
  onClose: () => void;
  onAdded: (message: string, folderId: number) => void;
}

const n = (v: number) => v.toLocaleString();

/**
 * Put a selection, or everything matching the current search (say, one
 * date range), into a folder. Attachments come along with their emails.
 */
export default function FolderDialog({ productionId, target, count, folders, onClose, onAdded }: Props) {
  const [folderId, setFolderId] = useState<number | 'new'>(folders[0]?.id ?? 'new');
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const canSubmit = !busy && (folderId !== 'new' || newName.trim().length > 0);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const folder = folderId === 'new'
        ? await createFolder(productionId, newName.trim())
        : folders.find(f => f.id === folderId)!;
      const res = await addToFolder(folder.id,
        target.kind === 'ids' ? { doc_ids: target.docIds } : { query: target.query });
      const parts = [`Added ${n(res.added)} to “${folder.name}”`];
      if (res.attachments_added) parts.push(`including ${n(res.attachments_added)} attachments`);
      if (res.already_in_folder) parts.push(`${n(res.already_in_folder)} were already there`);
      onAdded(`${parts.join(' — ')} (${n(res.document_count)} in folder)`, folder.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not add to the folder');
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel" onClick={e => e.stopPropagation()} style={{ maxWidth: 440 }}>
        <div className="modal-header">
          <h2 className="modal-title">Add to folder</h2>
          <button className="modal-close-btn" aria-label="Close" onClick={onClose}>&times;</button>
        </div>
        <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-600)' }}>
            {target.kind === 'query'
              ? `All ${n(count)} documents matching this search`
              : `${n(count)} selected document${count === 1 ? '' : 's'}`}
            , plus their attachments.
          </div>
          <fieldset style={{ border: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
            <legend className="visually-hidden">Folder</legend>
            {folders.map(f => (
              <label key={f.id} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <input type="radio" name="folder" checked={folderId === f.id} onChange={() => setFolderId(f.id)} />
                <span>{f.name}</span>
                <span style={{ marginLeft: 'auto', fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>
                  {n(f.document_count)}
                </span>
              </label>
            ))}
            <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input type="radio" name="folder" checked={folderId === 'new'} onChange={() => setFolderId('new')} />
              <input
                className="input input-sm"
                placeholder="New folder, e.g. March 2026"
                aria-label="New folder name"
                maxLength={200}
                value={newName}
                onFocus={() => setFolderId('new')}
                onChange={e => setNewName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && canSubmit) submit(); }}
                style={{ flex: 1 }}
              />
            </label>
          </fieldset>
          {error && <div role="alert" style={{ color: 'var(--color-danger, #b42318)', fontSize: 'var(--text-sm)' }}>{error}</div>}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
            <button className="btn btn-primary btn-sm" disabled={!canSubmit} onClick={submit}>
              {busy ? 'Adding…' : 'Add'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
