import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createSavedSearch, deleteSavedSearch, getSavedSearches } from '../api/client';
import { detectSearchMode, type SearchMode } from '../utils/searchMode';
import type { SavedSearch } from '../types';

interface Props {
  onSearch: (query: string, metadata?: Record<string, string>, forceMode?: SearchMode) => void;
  initialQuery?: string;
  onAsk?: (question: string) => void;
}

/**
 * Header search-or-ask box. Auto-detects full-text vs semantic ("ask") mode
 * as the user types and shows it as a clickable pill so the choice is
 * visible and overridable — the override applies to the next submit only.
 */
export default function Omnibox({ onSearch, initialQuery = '', onAsk }: Props) {
  const [query, setQuery] = useState(initialQuery);
  const [modeOverride, setModeOverride] = useState<SearchMode | null>(null);
  const [savedSearches, setSavedSearches] = useState<SavedSearch[]>([]);
  const [menu, setMenu] = useState<'none' | 'saved' | 'filters'>('none');
  const [saveName, setSaveName] = useState<string | null>(null);
  const [metadataFilters, setMetadataFilters] = useState<Record<string, string>>({});
  const [filterKey, setFilterKey] = useState('');
  const [filterValue, setFilterValue] = useState('');
  const [prevInitialQuery, setPrevInitialQuery] = useState(initialQuery);
  const rootRef = useRef<HTMLDivElement>(null);

  if (initialQuery !== prevInitialQuery) {
    setPrevInitialQuery(initialQuery);
    setQuery(initialQuery);
    setModeOverride(null);
  }

  const loadSaved = () => { getSavedSearches().then(setSavedSearches).catch(() => {}); };
  useEffect(loadSaved, []);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setMenu('none');
        setSaveName(null);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const mode: SearchMode = modeOverride ?? detectSearchMode(query);
  const filterCount = Object.keys(metadataFilters).length;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setMenu('none');
    onSearch(query.trim(), filterCount > 0 ? metadataFilters : undefined, mode);
    setModeOverride(null);
  };

  const handleSave = async () => {
    if (!saveName?.trim() || !query.trim()) return;
    await createSavedSearch(saveName.trim(), query.trim());
    setSaveName(null);
    loadSaved();
  };

  return (
    <div className="omnibox" ref={rootRef}>
      <form onSubmit={submit} className="omnibox-row">
        <input
          type="text"
          className="omnibox-input"
          value={query}
          onChange={e => { setQuery(e.target.value); setModeOverride(null); }}
          placeholder="Search, or ask a question…"
          aria-label="Search or ask a question"
        />
        {query.trim() && (
          <button
            type="button"
            className={`omnibox-mode ${mode === 'semantic' ? 'is-ask' : ''}`}
            onClick={() => setModeOverride(mode === 'semantic' ? 'fulltext' : 'semantic')}
            title="Toggle between full-text search and asking the production"
          >
            {mode === 'semantic' ? '✦ Ask' : 'Text'}
          </button>
        )}
        {mode === 'semantic' && onAsk && query.trim() && (
          <button
            type="button"
            className="omnibox-tool omnibox-ask"
            onClick={() => { onAsk(query.trim()); setModeOverride(null); }}
            title="Send this question to the AI chat"
          >
            ✦ Ask AI
          </button>
        )}
        <button
          type="button"
          className={`omnibox-tool ${filterCount > 0 ? 'is-active' : ''}`}
          onClick={() => setMenu(menu === 'filters' ? 'none' : 'filters')}
          title="Metadata filters"
        >
          Filters{filterCount > 0 ? ` (${filterCount})` : ''}
        </button>
        <button
          type="button"
          className="omnibox-tool"
          onClick={() => setMenu(menu === 'saved' ? 'none' : 'saved')}
          title="Saved searches"
        >
          Saved
        </button>
      </form>

      {menu === 'saved' && (
        <div className="dropdown omnibox-menu">
          {query.trim() && (
            saveName === null ? (
              <button type="button" className="dropdown-item" onClick={() => setSaveName(query)}>
                ＋ Save current search
              </button>
            ) : (
              <form
                className="omnibox-save-row"
                onSubmit={e => { e.preventDefault(); handleSave(); }}
              >
                <input
                  className="input input-sm"
                  value={saveName}
                  onChange={e => setSaveName(e.target.value)}
                  autoFocus
                  aria-label="Saved search name"
                />
                <button type="submit" className="btn btn-primary btn-xs">Save</button>
              </form>
            )
          )}
          {savedSearches.length === 0 && !query.trim() && (
            <div className="dropdown-item omnibox-empty">No saved searches yet.</div>
          )}
          {savedSearches.map(ss => (
            <div
              key={ss.id}
              className="dropdown-item omnibox-saved-item"
              onClick={() => { setQuery(ss.query); setMenu('none'); setModeOverride(null); onSearch(ss.query); }}
            >
              <div className="omnibox-saved-text">
                <div className="omnibox-saved-name">{ss.name}</div>
                <div className="omnibox-saved-query">{ss.query}</div>
              </div>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                aria-label={`Delete saved search ${ss.name}`}
                onClick={async e => { e.stopPropagation(); await deleteSavedSearch(ss.id); loadSaved(); }}
              >×</button>
            </div>
          ))}
        </div>
      )}

      {menu === 'filters' && (
        <div className="dropdown omnibox-menu">
          {Object.entries(metadataFilters).map(([k, v]) => (
            <div key={k} className="dropdown-item omnibox-saved-item">
              <span className="omnibox-saved-text">{k}: {v}</span>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                aria-label={`Remove filter ${k}`}
                onClick={() => {
                  const next = { ...metadataFilters };
                  delete next[k];
                  setMetadataFilters(next);
                }}
              >×</button>
            </div>
          ))}
          <form
            className="omnibox-save-row"
            onSubmit={e => {
              e.preventDefault();
              if (filterKey.trim() && filterValue.trim()) {
                setMetadataFilters({ ...metadataFilters, [filterKey.trim()]: filterValue.trim() });
                setFilterKey('');
                setFilterValue('');
              }
            }}
          >
            <input className="input input-sm" placeholder="Field" value={filterKey} onChange={e => setFilterKey(e.target.value)} aria-label="Filter field name" />
            <input className="input input-sm" placeholder="Value" value={filterValue} onChange={e => setFilterValue(e.target.value)} aria-label="Filter value" />
            <button type="submit" className="btn btn-secondary btn-xs">Add</button>
          </form>
        </div>
      )}
    </div>
  );
}
