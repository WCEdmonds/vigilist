import type { SearchResult, Tag } from '../types';
import { renderHighlightedSnippet } from '../utils/sanitize';

interface Props {
  results: SearchResult[];
  total: number;
  onSelect: (id: string) => void;
  selectedIds?: Set<string>;
  onToggleSelect?: (id: string) => void;
}

const COLOR_MAP: Record<string, string> = {
  green: 'badge-green', red: 'badge-red', yellow: 'badge-yellow',
  purple: 'badge-purple', gray: 'badge-gray', blue: 'badge-blue',
};

export default function SearchResults({ results, onSelect, selectedIds, onToggleSelect }: Props) {
  if (results.length === 0) {
    return <div className="empty-state" style={{ padding: 'var(--space-8)' }}>No results found</div>;
  }

  // The count lives in the section header's badge — repeating it here read
  // as clutter.
  return (
    <div>
      {results.map(r => (
        <div key={r.id} className="result-item" onClick={() => onSelect(r.id)}>
          <div className="result-header">
            {onToggleSelect && (
              <div className="checkbox-wrapper" onClick={e => e.stopPropagation()}>
                <input
                  type="checkbox"
                  checked={selectedIds?.has(r.id) || false}
                  onChange={() => onToggleSelect(r.id)}
                />
              </div>
            )}
            <span className="result-bates">{r.bates_begin}</span>
            {r.bates_begin !== r.bates_end && (
              <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)' }}>– {r.bates_end}</span>
            )}
            {r.tags?.map((tag: Tag) => (
              <span key={tag.id} className={`badge ${COLOR_MAP[tag.color] || 'badge-gray'}`}>{tag.name}</span>
            ))}
            <span className="result-meta">{r.page_count} pg{r.page_count !== 1 ? 's' : ''}</span>
          </div>
          {r.title && (
            <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)' }}>{r.title}</div>
          )}
          <div className="result-snippet">{renderHighlightedSnippet(r.snippet)}</div>
        </div>
      ))}
    </div>
  );
}
