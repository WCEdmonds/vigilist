import { useEffect, useState, type FormEvent } from 'react';
import { createNote, deleteNote, getNotes, updateNote } from '../api/client';
import type { NoteEntry } from '../types';
import { showToast } from './Toast';

interface Props {
  docId: string;
  mediaTime?: number | null;
  onSeek?: (time: number) => void;
}

export default function NotesPanel({ docId, mediaTime: _mediaTime, onSeek: _onSeek }: Props) {
  const [notes, setNotes] = useState<NoteEntry[]>([]);
  const [newContent, setNewContent] = useState('');
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editContent, setEditContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const loadNotes = async () => {
    setLoading(true);
    try {
      const res = await getNotes(docId);
      setNotes(res);
    } catch (e: any) {
      showToast(`Could not load notes: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadNotes(); }, [docId]);

  const handleAdd = async (e: FormEvent) => {
    e.preventDefault();
    if (!newContent.trim() || saving) return;
    setSaving(true);
    try {
      await createNote(docId, newContent.trim());
      setNewContent('');
      await loadNotes();
    } catch (e: any) {
      showToast(`Could not save note: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleUpdate = async (noteId: number) => {
    if (!editContent.trim() || saving) return;
    setSaving(true);
    try {
      await updateNote(noteId, editContent.trim());
      setEditingId(null);
      await loadNotes();
    } catch (e: any) {
      showToast(`Could not update note: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (noteId: number) => {
    if (deletingId !== null) return;
    if (!window.confirm('Delete this note? This cannot be undone.')) return;
    setDeletingId(noteId);
    try {
      await deleteNote(noteId);
      await loadNotes();
    } catch (e: any) {
      showToast(`Could not delete note: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setDeletingId(null);
    }
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ flex: 1, overflow: 'auto' }}>
        {loading && <div className="loading-center"><span className="spinner spinner-sm" /> Loading notes…</div>}
        {!loading && notes.length === 0 && (
          <div className="empty-state" style={{ padding: 'var(--space-8)' }}>No notes yet</div>
        )}
        {notes.map(note => (
          <div key={note.id} className="note-item">
            <div className="note-meta">
              <span>{note.created_by_display_name || note.created_by_email || note.created_by}</span>
              <span>·</span>
              <span>{formatDate(note.created_at)}</span>
              <div className="note-actions">
                <button className="btn btn-ghost btn-xs" onClick={() => { setEditingId(note.id); setEditContent(note.content); }} disabled={deletingId === note.id}>Edit</button>
                <button className="btn btn-ghost btn-xs" onClick={() => handleDelete(note.id)} disabled={deletingId === note.id}>
                  {deletingId === note.id ? 'Deleting…' : 'Delete'}
                </button>
              </div>
            </div>
            {editingId === note.id ? (
              <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-2)' }}>
                <textarea
                  className="input"
                  value={editContent}
                  onChange={e => setEditContent(e.target.value)}
                  rows={2}
                  autoFocus
                />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <button className="btn btn-primary btn-xs" onClick={() => handleUpdate(note.id)} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
                  <button className="btn btn-ghost btn-xs" onClick={() => setEditingId(null)} disabled={saving}>Cancel</button>
                </div>
              </div>
            ) : (
              <div className="note-content">{note.content}</div>
            )}
          </div>
        ))}
      </div>
      <form onSubmit={handleAdd} style={{ padding: 'var(--space-3)', borderTop: '1px solid var(--color-neutral-200)', display: 'flex', gap: 'var(--space-2)' }}>
        <label htmlFor="note-add-input" className="visually-hidden">Add a note</label>
        <input
          id="note-add-input"
          className="input input-sm"
          placeholder="Add a note..."
          value={newContent}
          onChange={e => setNewContent(e.target.value)}
          disabled={saving}
        />
        <button type="submit" className="btn btn-primary btn-sm" disabled={!newContent.trim() || saving}>
          {saving ? 'Adding…' : 'Add'}
        </button>
      </form>
    </div>
  );
}
