import { useCallback } from 'react';

interface Props {
  text: string | null;
  searchQuery?: string;
  onTitleChanged?: (title: string) => void;
}

export default function TextPanel({ text, searchQuery }: Props) {
  const copyToClipboard = useCallback(() => {
    if (text) navigator.clipboard.writeText(text);
  }, [text]);

  if (!text) {
    return <div className="empty-state">No extracted text available</div>;
  }

  let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  if (searchQuery) {
    const terms = searchQuery
      .replace(/["()]/g, '')
      .split(/\s+/)
      .filter(t => t && !['AND', 'OR', 'NOT'].includes(t.toUpperCase()));
    if (terms.length > 0) {
      const escaped = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\*$/, '\\w*'));
      const regex = new RegExp(`(${escaped.join('|')})`, 'gi');
      html = html.replace(regex, '<mark>$1</mark>');
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="panel-header">
        <span>Extracted Text</span>
        <button onClick={copyToClipboard} className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }}>
          Copy
        </button>
      </div>
      <div
        style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)', fontSize: 'var(--text-sm)', lineHeight: 1.65, whiteSpace: 'pre-wrap', fontFamily: 'var(--font-mono)' }}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}
