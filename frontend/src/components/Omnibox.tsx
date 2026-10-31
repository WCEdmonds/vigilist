import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createSavedSearch, deleteSavedSearch, getSavedSearches } from '../api/client';
import { detectSearchMode, type SearchMode } from '../utils/searchMode';
import { showToast } from './Toast';
import type { EmailScope, SavedSearch, SearchExtras } from '../types';

interface Props {
  onSearch: (query: string, metadata?: Record<string, string>, forceMode?: SearchMode, extras?: SearchExtras) => void;
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
  const [emailAddress, setEmailAddress] = useState('');
  const [emailScope, setEmailScope] = useState<EmailScope>('from');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
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

  // Esc closes whichever menu is open. Only listens while a menu is open.
  useEffect(() => {
    if (menu === 'none') return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setMenu('none'); setSaveName(null); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [menu]);

  const mode: SearchMode = modeOverride ?? detectSearchMode(query);
  const metadataCount = Object.keys(metadataFilters).length;
  const emailActive = emailAddress.trim().length > 0;
  const filterCount = metadataCount + (emailActive ? 1 : 0) + (dateFrom || dateTo ? 1 : 0);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const extras: SearchExtras = {};
    if (emailActive) extras.email = { address: emailAddress.trim(), scope: emailScope };
    if (dateFrom) extras.dateFrom = dateFrom;
    if (dateTo) extras.dateTo = dateTo;
    const hasExtras = !!(extras.email || extras.dateFrom || extras.dateTo);
    if (!query.trim() && !hasExtras) return;
    if (dateFrom && dateTo && dateFrom > dateTo) {
      showToast('The "from" date is after the "to" date', 'error');
      return;
    }
    setMenu('none');
    // Email/date filters always run full-text server-side; say so up front so
    // the results header and bulk selection agree with what the server did.
    onSearch(query.trim(), metadataCount > 0 ? metadataFilters : undefined,
      hasExtras ? 'fulltext' : mode, hasExtras ? extras : undefined);
    setModeOverride(null);
  };

  const handleSave = async () => {
    if (!saveName?.trim() || !query.trim()) return;
    try {
      await createSavedSearch(saveName.trim(), query.trim());
      setSaveName(null);
      loadSaved();
      showToast('Saved search created', 'success');
    } catch (e: unknown) {
      showToast(e instanceof Error ? e.message : 'Could not save search', 'error');
    }
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
            <div key={ss.id} className="omnibox-saved-item">
              <button
                type="button"
                className="dropdown-item"
                style={{ flex: 1 }}
                onClick={() => { setQuery(ss.query); setMenu('none'); setModeOverride(null); onSearch(ss.query); }}
              >
                <div className="omnibox-saved-text">
                  <div className="omnibox-saved-name">{ss.name}</div>
                  <div className="omnibox-saved-query">{ss.query}</div>
                </div>
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                aria-label={`Delete saved search ${ss.name}`}
                onClick={async e => {
                  e.stopPropagation();
                  try {
                    await deleteSavedSearch(ss.id);
                    loadSaved();
                    showToast('Saved search deleted', 'success');
                  } catch (err: unknown) {
                    showToast(err instanceof Error ? err.message : 'Could not delete saved search', 'error');
                  }
                }}
              >×</button>
            </div>
          ))}
        </div>
      )}

      {menu === 'filters' && (
        <div className="dropdown omnibox-menu">
          <form className="omnibox-save-row" onSubmit={submit} style={{ flexWrap: 'wrap' }}>
            <input
              className="input input-sm"
              placeholder="Email address or @domain"
              value={emailAddress}
              onChange={e => setEmailAddress(e.target.value)}
              aria-label="Email address filter"
              style={{ flex: '1 1 180px' }}
            />
            <div role="group" aria-label="Match the address in" style={{ display: 'flex', gap: 4 }}>
              <button
                type="button"
                className={`btn btn-xs ${emailScope === 'from' ? 'btn-primary' : 'btn-secondary'}`}
                aria-pressed={emailScope === 'from'}
                onClick={() => setEmailScope('from')}
              >Sender only</button>
              <button
                type="button"
                className={`btn btn-xs ${emailScope === 'any' ? 'btn-primary' : 'btn-secondary'}`}
                aria-pressed={emailScope === 'any'}
                onClick={() => setEmailScope('any')}
              >Any participant</button>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4, flex: '1 1 100%' }}>
              <label style={{ fontSize: 'var(--text-xs)' }} htmlFor="omnibox-date-from">From</label>
              <input id="omnibox-date-from" type="date" className="input input-sm"
                value={dateFrom} onChange={e => setDateFrom(e.target.value)} />
              <label style={{ fontSize: 'var(--text-xs)' }} htmlFor="omnibox-date-to">To</label>
              <input id="omnibox-date-to" type="date" className="input input-sm"
                value={dateTo} onChange={e => setDateTo(e.target.value)} />
            </div>
            {(emailActive || dateFrom || dateTo) && (
              <button type="button" className="btn btn-ghost btn-xs"
                onClick={() => { setEmailAddress(''); setDateFrom(''); setDateTo(''); }}>
                Clear
              </button>
            )}
            <button type="submit" className="btn btn-primary btn-xs">Search</button>
          </form>
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
