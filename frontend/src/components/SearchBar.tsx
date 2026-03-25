import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createSavedSearch, deleteSavedSearch, getSavedSearches, nlSearch } from '../api/client';
import type { SavedSearch } from '../types';

interface Props {
  onSearch: (query: string, metadata?: Record<string, string>) => void;
  onNlResults?: (results: { original_query: string; structured_query: string; results: unknown[]; total: number }) => void;
  initialQuery?: string;
}

export default function SearchBar({ onSearch, onNlResults, initialQuery = '' }: Props) {
  const [query, setQuery] = useState(initialQuery);
  const [nlMode, setNlMode] = useState(false);
  const [nlLoading, setNlLoading] = useState(false);
  const [savedSearches, setSavedSearches] = useState<SavedSearch[]>([]);
  const [showSaved, setShowSaved] = useState(false);
  const [showSaveModal, setShowSaveModal] = useState(false);
  const [saveName, setSaveName] = useState('');
  const savedRef = useRef<HTMLDivElement>(null);
  const [showFilters, setShowFilters] = useState(false);
  const [metadataFilters, setMetadataFilters] = useState<Record<string, string>>({});
  const [filterKey, setFilterKey] = useState('');
  const [filterValue, setFilterValue] = useState('');

  useEffect(() => { setQuery(initialQuery); }, [initialQuery]);

  const loadSaved = async () => {
    const res = await getSavedSearches();
    setSavedSearches(res);
  };

  useEffect(() => { loadSaved(); }, []);

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (savedRef.current && !savedRef.current.contains(e.target as Node)) {
        setShowSaved(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    if (nlMode && onNlResults) {
      setNlLoading(true);
      try {
        const res = await nlSearch(query.trim());
        onNlResults(res);
      } catch (err: any) {
        alert(`NL search error: ${err.message}`);
      } finally {
        setNlLoading(false);
      }
    } else {
      const filters = Object.keys(metadataFilters).length > 0 ? metadataFilters : undefined;
      onSearch(query.trim(), filters);
    }
  };

  const handleSave = async () => {
    if (!saveName.trim() || !query.trim()) return;
    await createSavedSearch(saveName.trim(), query.trim());
    setShowSaveModal(false);
    setSaveName('');
    loadSaved();
  };

  const handleDeleteSaved = async (id: number) => {
    await deleteSavedSearch(id);
    loadSaved();
  };

  return (
    <div className="search-toolbar">
      <form onSubmit={handleSubmit} className="search-row">
        <input
          type="text"
          className="input"
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder='Search documents... ("phrases", AND/OR/NOT, wildcard*)'
        />
        <button type="button" className={`btn btn-sm ${nlMode ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setNlMode(!nlMode)} title="Toggle AI natural language search">
          AI
        </button>
        <button type="submit" className="btn btn-primary" disabled={nlLoading}>
          {nlLoading ? 'Searching...' : nlMode ? 'AI Search' : 'Search'}
        </button>

        {query.trim() && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setShowSaveModal(true); setSaveName(query); }}>
            Save
          </button>
        )}

        {savedSearches.length > 0 && (
          <div style={{ position: 'relative' }} ref={savedRef}>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setShowSaved(!showSaved)}>
              Saved ({savedSearches.length})
            </button>
            {showSaved && (
              <div className="dropdown" style={{ top: '100%', right: 0, marginTop: 4, minWidth: 280 }}>
                <div className="saved-list">
                  {savedSearches.map(ss => (
                    <div key={ss.id} className="saved-item" onClick={() => { setQuery(ss.query); onSearch(ss.query); setShowSaved(false); }}>
                      <div style={{ flex: 1, overflow: 'hidden' }}>
                        <div style={{ fontWeight: 600 }}>{ss.name}</div>
                        <div className="query">{ss.query}</div>
                      </div>
                      <button
                        className="btn btn-ghost btn-xs delete-btn"
                        onClick={(e) => { e.stopPropagation(); handleDeleteSaved(ss.id); }}
                      >×</button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        <button type="button" className="btn btn-secondary" onClick={() => setShowFilters(!showFilters)}>
          Filters {Object.keys(metadataFilters).length > 0 && `(${Object.keys(metadataFilters).length})`}
        </button>
      </form>

      {showFilters && (
        <div style={{ padding: '0.5rem 0', display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' }}>
          {Object.entries(metadataFilters).map(([k, v]) => (
            <span key={k} style={{ background: '#e2e8f0', padding: '0.25rem 0.5rem', borderRadius: '4px', fontSize: '0.85em', display: 'inline-flex', alignItems: 'center', gap: '0.25rem' }}>
              {k}: {v}
              <button type="button" onClick={() => {
                const next = { ...metadataFilters };
                delete next[k];
                setMetadataFilters(next);
              }} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontSize: '1em' }}>&times;</button>
            </span>
          ))}
          <input placeholder="Field name" value={filterKey} onChange={e => setFilterKey(e.target.value)}
                 style={{ width: '120px', padding: '0.25rem 0.5rem', fontSize: '0.85em' }} />
          <input placeholder="Value" value={filterValue} onChange={e => setFilterValue(e.target.value)}
                 style={{ width: '120px', padding: '0.25rem 0.5rem', fontSize: '0.85em' }} />
          <button type="button" className="btn btn-secondary" onClick={() => {
            if (filterKey.trim() && filterValue.trim()) {
              setMetadataFilters({ ...metadataFilters, [filterKey.trim()]: filterValue.trim() });
              setFilterKey('');
              setFilterValue('');
            }
          }} style={{ fontSize: '0.85em', padding: '0.25rem 0.5rem' }}>Add</button>
        </div>
      )}

      {/* Save modal (inline) */}
      {showSaveModal && (
        <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', padding: 'var(--space-2) 0' }}>
          <span style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-600)' }}>Save as:</span>
          <input
            className="input input-sm"
            style={{ width: 220 }}
            value={saveName}
            onChange={e => setSaveName(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleSave(); }}
            autoFocus
          />
          <button className="btn btn-primary btn-sm" onClick={handleSave}>Save</button>
          <button className="btn btn-ghost btn-sm" onClick={() => setShowSaveModal(false)}>Cancel</button>
        </div>
      )}
    </div>
  );
}
