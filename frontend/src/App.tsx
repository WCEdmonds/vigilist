import { useCallback, useEffect, useState } from 'react';
import { bulkTag, exportDocsCsvUrl, exportSearchCsvUrl, getTags, imageUrl, listDocuments, listProductions, searchDocuments } from './api/client';
import DocumentViewer from './components/DocumentViewer';
import AuthPage from './components/AuthPage';
import IngestWizard from './components/IngestWizard';
import ManageAccess from './components/ManageAccess';
import SearchBar from './components/SearchBar';
import SearchResults from './components/SearchResults';
import { ToastContainer } from './components/Toast';
import WelcomePage from './components/WelcomePage';
import ProductionPicker from './components/ProductionPicker';
import UserAvatar from './components/UserAvatar';
import { AuthProvider, useAuth } from './hooks/useAuth';
import type { DocumentSummary, ProductionInfo, SearchResult, Tag } from './types';

const COLOR_MAP: Record<string, string> = {
  green: 'badge-green', red: 'badge-red', yellow: 'badge-yellow',
  purple: 'badge-purple', gray: 'badge-gray', blue: 'badge-blue',
};

type ViewMode = 'list' | 'grid';

interface HomeProps {
  production: ProductionInfo;
  onSwitchProduction: () => void;
  onIngestComplete: () => void;
}

function Home({ production, onSwitchProduction, onIngestComplete }: HomeProps) {
  const { user, logout } = useAuth();
  const [viewDocId, setViewDocId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchTotal, setSearchTotal] = useState(0);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [docTotal, setDocTotal] = useState(0);
  const [docPage, setDocPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [showBulkTagPicker, setShowBulkTagPicker] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>('list');
  const [hideNativeOnly, setHideNativeOnly] = useState(true);
  const [showManageAccess, setShowManageAccess] = useState(false);
  const [showIngestWizard, setShowIngestWizard] = useState(false);

  const perPage = 50;

  useEffect(() => {
    loadDocuments();
    getTags().then(setAllTags).catch(() => {});
  }, [production.id]);

  const loadDocuments = async (page = 1) => {
    setLoading(true);
    try {
      const res = await listDocuments(page, perPage, production.id);
      setDocuments(res.documents);
      setDocTotal(res.total);
      setDocPage(page);
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = async (query: string, metadata?: Record<string, string>) => {
    setLoading(true);
    setSearchQuery(query);
    setHasSearched(true);
    setSelectedIds(new Set());
    try {
      const res = await searchDocuments(query, 1, perPage, 'relevance', production.id, undefined, metadata);
      setSearchResults(res.results);
      setSearchTotal(res.total);
    } finally {
      setLoading(false);
    }
  };

  const clearSearch = () => {
    setHasSearched(false);
    setSearchQuery('');
    setSearchResults([]);
    setSelectedIds(new Set());
    loadDocuments();
  };

  const toggleSelect = useCallback((id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleBulkTag = async (tagId: number) => {
    if (selectedIds.size === 0) return;
    await bulkTag(Array.from(selectedIds), [tagId]);
    setSelectedIds(new Set());
    setShowBulkTagPicker(false);
    if (hasSearched) handleSearch(searchQuery);
    else loadDocuments(docPage);
  };

  // Document viewer mode
  if (viewDocId) {
    return (
      <DocumentViewer
        docId={viewDocId}
        onNavigate={setViewDocId}
        onBack={() => setViewDocId(null)}
        searchQuery={searchQuery}
        onSearch={(q) => { setViewDocId(null); handleSearch(q); }}
      />
    );
  }

  const displayDocs = hideNativeOnly
    ? documents.filter(d => !d.has_native || d.page_count > 0)
    : documents;
  const totalPages = Math.ceil(docTotal / perPage);

  return (
    <div style={{ minHeight: '100vh', background: 'var(--color-neutral-50)' }}>
      {/* Header */}
      <div className="app-header">
        <span className="logo" onClick={clearSearch}>
          Vigilist
        </span>
        <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-primary-300)', opacity: 0.7 }}>/</span>
        <span style={{ fontSize: 'var(--text-sm)', color: 'var(--color-primary-200)', cursor: 'pointer' }} onClick={onSwitchProduction}>
          {production.name}
        </span>
        {production.is_owner && (
          <button className="btn-header" onClick={() => setShowManageAccess(true)}>Share</button>
        )}
        <div className="user-menu">
          <button className="btn-header" onClick={() => setShowIngestWizard(true)}>+ Ingest</button>
          <UserAvatar name={user?.displayName ?? null} email={user?.email ?? ''} size={26} />
          <span style={{ opacity: 0.7 }}>{user?.displayName || user?.email}</span>
          <button className="btn-header" onClick={logout}>Sign out</button>
        </div>
      </div>

      {/* Content */}
      <div className="content-area" style={{ paddingTop: 'var(--space-4)', paddingBottom: 'var(--space-8)' }}>
        <SearchBar onSearch={handleSearch} initialQuery={searchQuery} />

        {loading && (
          <div className="loading-center">
            <span className="spinner spinner-md" />
            <span>Loading...</span>
          </div>
        )}

        {/* Search results */}
        {hasSearched && !loading && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: 'var(--space-2) 0' }}>
              <button className="btn btn-ghost btn-sm" onClick={clearSearch}>
                ← All documents
              </button>
              <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', fontFamily: 'var(--font-mono)' }}>
                {searchTotal} result{searchTotal !== 1 ? 's' : ''}
              </span>
              <a href={exportSearchCsvUrl(searchQuery)} className="btn btn-ghost btn-sm" download style={{ marginLeft: 'auto', textDecoration: 'none' }}>
                Export CSV
              </a>
            </div>
            <div className="card">
              <SearchResults
                results={searchResults}
                total={searchTotal}
                onSelect={setViewDocId}
                selectedIds={selectedIds}
                onToggleSelect={toggleSelect}
              />
            </div>
          </>
        )}

        {/* Document browse list */}
        {!hasSearched && !loading && displayDocs.length > 0 && (
          <div>
            <div className="section-header">
              <h2 className="section-title">
                All Documents
                <span className="section-count">{docTotal}</span>
              </h2>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
                <a href={exportDocsCsvUrl()} className="btn btn-ghost btn-sm" download style={{ textDecoration: 'none' }}>
                  Export CSV
                </a>
                <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-1)', fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', cursor: 'pointer' }}>
                  <input type="checkbox" checked={hideNativeOnly} onChange={e => setHideNativeOnly(e.target.checked)} style={{ accentColor: 'var(--color-primary-800)' }} />
                  Hide native-only
                </label>
                <div className="view-toggle">
                  <button
                    className={`view-toggle-btn ${viewMode === 'list' ? 'active' : ''}`}
                    onClick={() => setViewMode('list')}
                    title="List view"
                  >
                    List
                  </button>
                  <button
                    className={`view-toggle-btn ${viewMode === 'grid' ? 'active' : ''}`}
                    onClick={() => setViewMode('grid')}
                    title="Grid view"
                  >
                    Grid
                  </button>
                </div>
              </div>
            </div>

            {viewMode === 'list' ? (
              <div className="card" style={{ overflow: 'hidden' }}>
                <table className="doc-table">
                  <thead>
                    <tr>
                      <th style={{ width: 40 }}>
                        <div className="checkbox-wrapper">
                          <input
                            type="checkbox"
                            checked={displayDocs.length > 0 && displayDocs.every(d => selectedIds.has(d.id))}
                            onChange={() => {
                              const allSelected = displayDocs.every(d => selectedIds.has(d.id));
                              if (allSelected) setSelectedIds(new Set());
                              else setSelectedIds(new Set(displayDocs.map(d => d.id)));
                            }}
                          />
                        </div>
                      </th>
                      <th>Bates Range</th>
                      <th>Title</th>
                      <th>Pages</th>
                      <th>Tags</th>
                      <th>Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {displayDocs.map(d => (
                      <tr key={d.id} onClick={() => setViewDocId(d.id)}>
                        <td onClick={e => e.stopPropagation()}>
                          <div className="checkbox-wrapper">
                            <input
                              type="checkbox"
                              checked={selectedIds.has(d.id)}
                              onChange={() => toggleSelect(d.id)}
                            />
                          </div>
                        </td>
                        <td className="bates-cell">
                          {d.bates_begin}
                          {d.bates_begin !== d.bates_end && (
                            <span style={{ fontWeight: 'normal', color: 'var(--color-neutral-400)', marginLeft: 'var(--space-2)' }}>
                              – {d.bates_end}
                            </span>
                          )}
                        </td>
                        <td style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)', maxWidth: 340, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {d.title || '—'}
                        </td>
                        <td className="meta-cell">{d.page_count}</td>
                        <td>
                          <div className="tags-cell">
                            {d.tags?.map(tag => (
                              <span key={tag.id} className={`badge ${COLOR_MAP[tag.color] || 'badge-gray'}`}>
                                {tag.name}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td className="meta-cell">
                          {d.note_count > 0 && (
                            <span className="badge badge-blue">{d.note_count}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              /* Grid view */
              <div className="doc-grid">
                {displayDocs.map(d => (
                  <div key={d.id} className="doc-grid-card card" onClick={() => setViewDocId(d.id)}>
                    <div className="doc-grid-thumb">
                      <img src={imageUrl(d.id, 1)} alt={d.bates_begin} loading="lazy" />
                    </div>
                    <div className="doc-grid-info">
                      <div className="doc-grid-bates">{d.bates_begin}</div>
                      {d.title && <div className="doc-grid-title">{d.title}</div>}
                      <div className="doc-grid-meta">
                        {d.page_count} pg{d.page_count !== 1 ? 's' : ''}
                        {d.tags?.map(tag => (
                          <span key={tag.id} className={`badge ${COLOR_MAP[tag.color] || 'badge-gray'}`} style={{ marginLeft: 4 }}>
                            {tag.name}
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="pagination">
                <button
                  className="btn btn-secondary btn-sm"
                  disabled={docPage <= 1}
                  onClick={() => loadDocuments(docPage - 1)}
                >
                  ← Prev
                </button>
                <span className="page-info">
                  Page {docPage} of {totalPages}
                </span>
                <button
                  className="btn btn-secondary btn-sm"
                  disabled={docPage >= totalPages}
                  onClick={() => loadDocuments(docPage + 1)}
                >
                  Next →
                </button>
              </div>
            )}
          </div>
        )}

        {!hasSearched && !loading && displayDocs.length === 0 && (
          <div className="empty-state">
            <div style={{ fontSize: 'var(--text-lg)', fontFamily: 'var(--font-serif)', color: 'var(--color-neutral-500)' }}>No documents yet</div>
            <div>Ingest a production to get started</div>
            <button className="btn btn-primary" onClick={() => setShowIngestWizard(true)}>
              Ingest a Production
            </button>
          </div>
        )}
      </div>

      {/* Manage access modal */}
      {showManageAccess && (
        <ManageAccess
          productionId={production.id}
          onClose={() => setShowManageAccess(false)}
        />
      )}

      {/* Ingest wizard modal */}
      {showIngestWizard && (
        <IngestWizard
          onClose={() => setShowIngestWizard(false)}
          onComplete={() => { setShowIngestWizard(false); onIngestComplete(); }}
        />
      )}

      {/* Floating bulk action bar */}
      {selectedIds.size > 0 && (
        <div className="floating-bar">
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--text-xs)' }}>{selectedIds.size} selected</span>
          <div className="divider" style={{ background: 'rgba(255,255,255,0.15)', height: 20 }} />
          <div style={{ position: 'relative' }}>
            <button
              className="btn btn-sm btn-secondary"
              onClick={() => setShowBulkTagPicker(!showBulkTagPicker)}
            >
              Tag
            </button>
            {showBulkTagPicker && (
              <div className="dropdown" style={{ bottom: '100%', left: 0, marginBottom: 8, minWidth: 220 }}>
                {allTags.map(tag => (
                  <div
                    key={tag.id}
                    className="dropdown-item"
                    onClick={() => handleBulkTag(tag.id)}
                  >
                    <span className={`badge ${COLOR_MAP[tag.color] || 'badge-gray'}`}>{tag.name}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
          <button
            className="btn btn-sm btn-ghost"
            style={{ color: 'var(--color-neutral-300)' }}
            onClick={() => { setSelectedIds(new Set()); setShowBulkTagPicker(false); }}
          >
            Clear
          </button>
        </div>
      )}
    </div>
  );
}

function AppRouter() {
  const [productions, setProductions] = useState<ProductionInfo[]>([]);
  const [activeProduction, setActiveProduction] = useState<ProductionInfo | null>(null);
  const [prodLoading, setProdLoading] = useState(true);
  const [showIngestWizard, setShowIngestWizard] = useState(false);

  const loadProductions = async () => {
    setProdLoading(true);
    try {
      const prods = await listProductions();
      setProductions(prods);
      if (prods.length === 1) setActiveProduction(prods[0]);
      else if (prods.length === 0) setActiveProduction(null);
    } catch {}
    setProdLoading(false);
  };

  useEffect(() => { loadProductions(); }, []);

  const handleIngestComplete = () => {
    setActiveProduction(null);
    loadProductions();
  };

  if (prodLoading) {
    return <div className="loading-center"><span className="spinner spinner-md" /> Loading...</div>;
  }

  if (productions.length === 0) {
    return (
      <>
        <WelcomePage onIngest={() => setShowIngestWizard(true)} />
        {showIngestWizard && (
          <IngestWizard onClose={() => setShowIngestWizard(false)} onComplete={handleIngestComplete} />
        )}
        <ToastContainer />
      </>
    );
  }

  if (!activeProduction) {
    return (
      <>
        <ProductionPicker
          productions={productions}
          onSelect={setActiveProduction}
          onIngest={() => setShowIngestWizard(true)}
        />
        {showIngestWizard && (
          <IngestWizard onClose={() => setShowIngestWizard(false)} onComplete={handleIngestComplete} />
        )}
        <ToastContainer />
      </>
    );
  }

  return (
    <>
      <Home
        production={activeProduction}
        onSwitchProduction={() => setActiveProduction(null)}
        onIngestComplete={handleIngestComplete}
      />
      <ToastContainer />
    </>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  );
}

function AppContent() {
  const { user, loading } = useAuth();
  if (loading) return <div className="loading-center"><span className="spinner spinner-md" /> Loading...</div>;
  if (!user) return <AuthPage />;
  return <AppRouter />;
}
