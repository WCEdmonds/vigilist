import { Fragment, useCallback, useMemo, type ReactNode } from 'react';

interface Props {
  text: string | null;
  searchQuery?: string;
  onTitleChanged?: (title: string) => void;
}

function highlightTerms(text: string, searchQuery?: string): ReactNode {
  if (!searchQuery) return text;
  const terms = searchQuery
    .replace(/["()]/g, '')
    .split(/\s+/)
    .filter(t => t && !['AND', 'OR', 'NOT'].includes(t.toUpperCase()));
  if (terms.length === 0) return text;

  const escaped = terms.map(t =>
    t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\*$/, '\\w*'),
  );
  const regex = new RegExp(`(${escaped.join('|')})`, 'gi');
  const parts = text.split(regex);
  return parts.map((part, i) => {
    if (i % 2 === 1) return <mark key={i}>{part}</mark>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}

export default function TextPanel({ text, searchQuery }: Props) {
  const copyToClipboard = useCallback(() => {
    if (text) navigator.clipboard.writeText(text);
  }, [text]);

  const highlighted = useMemo(
    () => (text ? highlightTerms(text, searchQuery) : null),
    [text, searchQuery],
  );

  if (!text) {
    return <div className="empty-state">No extracted text available</div>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="panel-header">
        <span>Extracted Text</span>
        <button onClick={copyToClipboard} className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }} aria-label="Copy extracted text to clipboard">
          Copy
        </button>
      </div>
      <div
        style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)', fontSize: 'var(--text-sm)', lineHeight: 1.65, whiteSpace: 'pre-wrap', fontFamily: 'var(--font-mono)' }}
      >
        {highlighted}
      </div>
    </div>
  );
}
