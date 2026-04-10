import { useEffect, useState, type FormEvent } from 'react';
import { createNote, deleteNote, getNotes, updateNote } from '../api/client';
import type { NoteEntry } from '../types';

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

  const loadNotes = async () => {
    setLoading(true);
    try {
      const res = await getNotes(docId);
      setNotes(res);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadNotes(); }, [docId]);

  const handleAdd = async (e: FormEvent) => {
    e.preventDefault();
    if (!newContent.trim()) return;
    await createNote(docId, newContent.trim());
    setNewContent('');
    loadNotes();
  };

  const handleUpdate = async (noteId: number) => {
    if (!editContent.trim()) return;
    await updateNote(noteId, editContent.trim());
    setEditingId(null);
    loadNotes();
  };

  const handleDelete = async (noteId: number) => {
    await deleteNote(noteId);
    loadNotes();
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ flex: 1, overflow: 'auto' }}>
        {loading && <div className="loading-center"><span className="spinner spinner-sm" /></div>}
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
                <button className="btn btn-ghost btn-xs" onClick={() => { setEditingId(note.id); setEditContent(note.content); }}>Edit</button>
                <button className="btn btn-ghost btn-xs" onClick={() => handleDelete(note.id)}>Delete</button>
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
                  <button className="btn btn-primary btn-xs" onClick={() => handleUpdate(note.id)}>Save</button>
                  <button className="btn btn-ghost btn-xs" onClick={() => setEditingId(null)}>Cancel</button>
                </div>
              </div>
            ) : (
              <div className="note-content">{note.content}</div>
            )}
          </div>
        ))}
      </div>
      <form onSubmit={handleAdd} style={{ padding: 'var(--space-3)', borderTop: '1px solid var(--color-neutral-200)', display: 'flex', gap: 'var(--space-2)' }}>
        <input
          className="input input-sm"
          placeholder="Add a note..."
          value={newContent}
          onChange={e => setNewContent(e.target.value)}
        />
        <button type="submit" className="btn btn-primary btn-sm" disabled={!newContent.trim()}>Add</button>
      </form>
    </div>
  );
}
