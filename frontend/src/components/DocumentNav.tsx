import { useCallback, useEffect, useState } from 'react';
import { getByBates, getDocumentNav } from '../api/client';
import type { DocumentDetail } from '../types';

interface Props {
  doc: DocumentDetail;
  onNavigate: (id: string) => void;
  searchQuery?: string;
}

export default function DocumentNav({ doc, onNavigate, searchQuery }: Props) {
  const [prevId, setPrevId] = useState<string | null>(null);
  const [nextId, setNextId] = useState<string | null>(null);
  const [batesInput, setBatesInput] = useState('');

  useEffect(() => {
    getDocumentNav(doc.id).then(nav => {
      setPrevId(nav.prev_id);
      setNextId(nav.next_id);
    });
  }, [doc.id]);

  const goPrev = useCallback(() => { if (prevId) onNavigate(prevId); }, [prevId, onNavigate]);
  const goNext = useCallback(() => { if (nextId) onNavigate(nextId); }, [nextId, onNavigate]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === 'ArrowLeft') goPrev();
      else if (e.key === 'ArrowRight') goNext();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [goPrev, goNext]);

  const jumpToBates = async () => {
    if (!batesInput.trim()) return;
    try {
      const found = await getByBates(batesInput.trim());
      onNavigate(found.id);
      setBatesInput('');
    } catch {
      // Could show a toast, for now just shake the input
    }
  };

  return (
    <div className="doc-nav">
      <button className="btn btn-secondary btn-sm" onClick={goPrev} disabled={!prevId}>← Prev</button>
      <span className="bates-label">{doc.bates_begin}</span>
      {doc.bates_begin !== doc.bates_end && (
        <span className="bates-range">– {doc.bates_end}</span>
      )}
      <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)' }}>
        {doc.page_count} pg{doc.page_count !== 1 ? 's' : ''}
      </span>
      {doc.title && (
        <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginLeft: 'var(--space-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {doc.title}
        </span>
      )}
      <button className="btn btn-secondary btn-sm" onClick={goNext} disabled={!nextId}>Next →</button>

      {searchQuery && (
        <span className="breadcrumb">
          Search: "{searchQuery}"
        </span>
      )}

      <div className="jump-bates">
        <input
          type="text"
          className="input input-sm"
          placeholder="Jump to Bates..."
          value={batesInput}
          onChange={e => setBatesInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') jumpToBates(); }}
          style={{ width: 150 }}
        />
        <button className="btn btn-secondary btn-sm" onClick={jumpToBates}>Go</button>
      </div>
    </div>
  );
}
