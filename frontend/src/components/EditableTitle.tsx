import { useRef, useState } from 'react';
import { updateDocTitle } from '../api/client';

interface Props {
  docId: string;
  title: string | null;
  onUpdated?: (title: string | null) => void;
}

export default function EditableTitle({ docId, title, onUpdated }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title || '');
  const [hover, setHover] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const saving = useRef(false);
  const save = async () => {
    if (saving.current) return;
    saving.current = true;
    const newTitle = draft.trim();
    setEditing(false);
    if (newTitle === (title || '')) { saving.current = false; return; }
    try {
      const res = await updateDocTitle(docId, newTitle);
      onUpdated?.(res.title);
    } catch {
      setDraft(title || '');
    } finally {
      saving.current = false;
    }
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="input input-sm"
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={e => {
          e.stopPropagation();
          if (e.key === 'Enter') { e.preventDefault(); inputRef.current?.blur(); }
          if (e.key === 'Escape') { setEditing(false); setDraft(title || ''); }
        }}
        onClick={e => e.stopPropagation()}
        onMouseDown={e => e.stopPropagation()}
        style={{ fontSize: 'var(--text-xs)', width: '100%' }}
        autoFocus
      />
    );
  }

  return (
    <span
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={e => {
        e.stopPropagation();
        setDraft(title || '');
        setEditing(true);
      }}
      style={{
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        gap: 'var(--space-1)',
      }}
    >
      {title || '—'}
      {hover && <span style={{ fontSize: 10, opacity: 0.5 }}>✎</span>}
    </span>
  );
}
