import { useCallback, useEffect, useMemo, useState } from 'react';
import { bulkTag, createTag, exportDocsCsv, exportSearchCsv, fetchBulkZip, getClusters, getMyBatches, getTags, listDocuments, listProductions, searchDocuments } from './api/client';
import DocumentViewer from './components/DocumentViewer';
import AuthImage from './components/AuthImage';
import AIAgent, { type AttachedDoc } from './components/AIAgent';
import AIReviewPage from './components/AIReviewPage';
import CorpusAnalysis from './components/CorpusAnalysis';
import AuthPage from './components/AuthPage';
import EditableTitle from './components/EditableTitle';
import IngestWizard from './components/IngestWizard';
import AuditLog from './components/AuditLog';
import ManageAccess from './components/ManageAccess';
import QueueManager from './components/QueueManager';
import BatchReview from './components/BatchReview';
import Dashboard from './components/Dashboard';
import SearchBar from './components/SearchBar';
import SearchResults from './components/SearchResults';
import TopicGroups from './components/TopicGroups';
import { ToastContainer, showToast } from './components/Toast';
import WelcomePage from './components/WelcomePage';
import ProductionPicker from './components/ProductionPicker';
import UserAvatar from './components/UserAvatar';
import { AuthProvider, useAuth } from './hooks/useAuth';
import { getInitialUrlState, useSyncUrl } from './hooks/useUrlState';
import type { ClusterInfo, DocumentSummary, ProductionInfo, ReviewBatch, SearchResult, Tag } from './types';

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
  const initialUrl = useMemo(() => getInitialUrlState(), []);
  const [viewDocId, setViewDocId] = useState<string | null>(initialUrl.doc ?? null);
  const [searchQuery, setSearchQuery] = useState(initialUrl.q ?? '');
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchTotal, setSearchTotal] = useState(0);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [docTotal, setDocTotal] = useState(0);
  const [docPage, setDocPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(!!initialUrl.q);
  const [lastSearchMode, setLastSearchMode] = useState<'fulltext' | 'semantic'>('fulltext');
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [showBulkTagPicker, setShowBulkTagPicker] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>('list');
  const [filterTagId, setFilterTagId] = useState<number | null>(null);
  const [filterFileType, setFilterFileType] = useState<string>('');
  const [sortBy, setSortBy] = useState<string>('bates');

  const [showManageAccess, setShowManageAccess] = useState(false);
  const [showAuditLog, setShowAuditLog] = useState(false);
  const [showIngestWizard, setShowIngestWizard] = useState(false);
  const [showQueueManager, setShowQueueManager] = useState(false);
  const [showDashboard, setShowDashboard] = useState(false);
  const [activeBatchId, setActiveBatchId] = useState<number | null>(
    initialUrl.batch ? Number(initialUrl.batch) : null,
  );
  const [myBatches, setMyBatches] = useState<ReviewBatch[]>([]);
  const [showAIReview, setShowAIReview] = useState(initialUrl.view === 'ai');
  const [showCorpusAnalysis, setShowCorpusAnalysis] = useState(initialUrl.view === 'analysis');

  // AI Agent chat (session-only) — floating launcher + "Send to AI Agent".
  const [chatOpen, setChatOpen] = useState(false);
  const [chatDocs, setChatDocs] = useState<AttachedDoc[]>([]);

  // Attach the currently-selected documents to the AI agent and open the panel.
  const sendSelectionToAgent = useCallback(() => {
    const labelFor = (id: string): string => {
      const fromSearch = searchResults.find(r => r.id === id);
      if (fromSearch) return fromSearch.bates_begin;
      const fromDocs = documents.find(d => d.id === id);
      return fromDocs?.bates_begin ?? id.slice(0, 8);
    };
    const docs: AttachedDoc[] = Array.from(selectedIds).map(id => ({ id, label: labelFor(id) }));
    setChatDocs(prev => {
      // Merge with anything already attached, de-duplicating by id.
      const seen = new Set(prev.map(d => d.id));
      return [...prev, ...docs.filter(d => !seen.has(d.id))];
    });
    setChatOpen(true);
  }, [selectedIds, searchResults, documents]);

  // Mirror the key bits of state back into the URL so a refresh lands
  // the user on the same page (doc viewer, batch review, search, etc.).
  useSyncUrl({
    prod: String(production.id),
    doc: viewDocId ?? undefined,
    q: hasSearched ? searchQuery || undefined : undefined,
    batch: activeBatchId ? String(activeBatchId) : undefined,
    view: showAIReview ? 'ai' : showCorpusAnalysis ? 'analysis' : undefined,
  });

  // If a search query was in the URL on mount, run the search once.
  useEffect(() => {
    if (initialUrl.q && !searchResults.length) {
      handleSearch(initialUrl.q);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [clusters, setClusters] = useState<ClusterInfo[]>([]);
  const [filterClusterId, setFilterClusterId] = useState<number | null>(null);

  const [perPage, setPerPage] = useState(50);

  useEffect(() => {
    loadDocuments();
    getTags().then(setAllTags).catch(e => console.warn('getTags failed:', e));
    getMyBatches(production.id).then(setMyBatches).catch(e => console.warn('getMyBatches failed:', e));
    getClusters(production.id).then(setClusters).catch(e => console.warn('getClusters failed:', e));
  }, [production.id, perPage, filterTagId, filterFileType, sortBy, filterClusterId]);

  const loadDocuments = async (page = 1) => {
    setLoading(true);
    try {
      const res = await listDocuments(page, perPage, production.id, filterTagId ?? undefined, filterFileType || undefined, sortBy, filterClusterId ?? undefined);
      setDocuments(res.documents);
      setDocTotal(res.total);
      setDocPage(page);
    } catch (e: any) {
      showToast(`Could not load documents: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = async (query: string, _metadata?: Record<string, string>, forceMode?: 'fulltext' | 'semantic') => {
    setLoading(true);
    setSearchQuery(query);
    setHasSearched(true);
    setSelectedIds(new Set());

    // Use forced mode if provided, otherwise auto-detect
    const mode = forceMode ?? (
      query.length > 40
        || /\b(what|where|who|when|why|how|which|find|show|any|all)\b/i.test(query)
        || query.includes('?')
      ? 'semantic' : 'fulltext'
    );
    setLastSearchMode(mode);

    try {
      const res = await searchDocuments(
        query, 1, perPage, 'relevance', production.id,
        undefined, undefined, mode, filterFileType || undefined,
      );
      setSearchResults(res.results);
      setSearchTotal(res.total);
    } catch (e: any) {
      showToast(`Search failed: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  // Re-run the active search whenever the file-type filter changes.
  useEffect(() => {
    if (hasSearched) handleSearch(searchQuery, undefined, lastSearchMode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterFileType]);

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

  const [newTagName, setNewTagName] = useState('');
  const [bulkDownloading, setBulkDownloading] = useState(false);
  const [bulkTagging, setBulkTagging] = useState(false);

  const handleBulkTag = async (tagId: number) => {
    if (selectedIds.size === 0 || bulkTagging) return;
    setBulkTagging(true);
    try {
      await bulkTag(Array.from(selectedIds), [tagId]);
      setSelectedIds(new Set());
      setShowBulkTagPicker(false);
      showToast(`Tagged ${selectedIds.size} document${selectedIds.size === 1 ? '' : 's'}`, 'success');
      if (hasSearched) handleSearch(searchQuery);
      else loadDocuments(docPage);
    } catch (e: any) {
      showToast(`Could not apply tag: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setBulkTagging(false);
    }
  };

  const handleBulkCreateTag = async () => {
    const name = newTagName.trim();
    if (!name || bulkTagging) return;
    setBulkTagging(true);
    try {
      const tag = await createTag({ name, category: 'custom', color: 'blue' });
      setAllTags(prev => [...prev, tag]);
      await bulkTag(Array.from(selectedIds), [tag.id]);
      setSelectedIds(new Set());
      setShowBulkTagPicker(false);
      setNewTagName('');
      showToast(`Created tag "${name}" and applied to ${selectedIds.size} document${selectedIds.size === 1 ? '' : 's'}`, 'success');
      if (hasSearched) handleSearch(searchQuery);
      else loadDocuments(docPage);
    } catch (e: any) {
      showToast(`Could not create tag: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setBulkTagging(false);
    }
  };

  const handleBulkDownload = async () => {
    if (selectedIds.size === 0 || bulkDownloading) return;
    setBulkDownloading(true);
    try {
      const blob = await fetchBulkZip(Array.from(selectedIds));
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'vigilist_documents.zip';
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      showToast(`Downloaded ${selectedIds.size} document${selectedIds.size === 1 ? '' : 's'}`, 'success');
    } catch (e: any) {
      showToast(`Could not build download: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setBulkDownloading(false);
    }
  };

  // AI Review full-screen mode
  if (showAIReview) {
    return <AIReviewPage productionId={production.id} onViewDocument={(id) => { setShowAIReview(false); setViewDocId(id); }} onBack={() => setShowAIReview(false)} />;
  }

  // Corpus Analysis full-screen mode
  if (showCorpusAnalysis) {
    return <CorpusAnalysis productionId={production.id} onViewDocument={(id) => { setShowCorpusAnalysis(false); setViewDocId(id); }} onFilterCluster={() => {}} onBack={() => setShowCorpusAnalysis(false)} />;
  }

  // Batch review full-screen mode
  if (activeBatchId) {
    return (
      <BatchReview
        batchId={activeBatchId}
        onClose={() => setActiveBatchId(null)}
        onComplete={() => {
          setActiveBatchId(null);
          getMyBatches(production.id).then(setMyBatches).catch(e => console.warn('getMyBatches failed:', e));
        }}
      />
    );
  }

  // Build the current doc ID list for nav (search results or filtered docs)
  const currentDocIds = hasSearched
    ? searchResults.map(r => r.id)
    : documents.map(d => d.id);

  // Document viewer mode
  if (viewDocId) {
    return (
      <DocumentViewer
        docId={viewDocId}
        onNavigate={setViewDocId}
        onBack={() => setViewDocId(null)}
        searchQuery={searchQuery}
        onSearch={(q) => { setViewDocId(null); handleSearch(q); }}
        onSimilarResults={(label, results) => {
          setViewDocId(null);
          setSearchQuery(label);
          setHasSearched(true);
          setSearchResults(results);
          setSearchTotal(results.length);
        }}
        docIds={currentDocIds}
      />
    );
  }

  const displayDocs = documents;
  const totalPages = Math.ceil(docTotal / perPage);

  return (
    <div style={{ minHeight: '100vh', background: 'var(--color-neutral-50)' }}>
      {/* Header */}
      <div className="app-header">
        <span className="logo" onClick={clearSearch}>
          Vigilist
        </span>
        <span style={{ fontSize: 'var(--text-xs)', color: 'rgba(44, 62, 107, 0.25)', margin: '0 2px' }}>/</span>
        <span style={{ fontSize: 'var(--text-sm)', fontWeight: 600, color: 'var(--color-ink)', cursor: 'pointer', marginRight: 'var(--space-3)' }} onClick={onSwitchProduction}>
          {production.name}
        </span>
        <div className="desktop-only" style={{ display: 'flex', gap: 4, background: 'rgba(44, 62, 107, 0.05)', borderRadius: 'var(--radius-md)', padding: 3 }}>
          {production.is_owner && (
            <button className="btn-header" style={{ background: 'rgba(255,255,255,0.7)' }} onClick={() => setShowManageAccess(true)}>Share</button>
          )}
          <button className="btn-header" style={{ background: 'rgba(255,255,255,0.7)', display: 'flex', alignItems: 'center', gap: 6 }} onClick={() => setShowAIReview(true)}>
            <span className="ai-indicator" style={{ padding: '0 4px', fontSize: 9 }}>AI</span>
            Smart Review
          </button>
          <button className="btn-header" style={{ background: 'rgba(255,255,255,0.7)' }} onClick={() => setShowQueueManager(true)}>Review Queues</button>
          <button className="btn-header" style={{ background: 'rgba(255,255,255,0.7)' }} onClick={() => setShowDashboard(true)}>Dashboard</button>
        </div>
        <div className="user-menu">
          <span className="desktop-only" style={{ display: 'contents' }}>
            {production.is_owner && (
              <button className="btn-header" onClick={() => setShowAuditLog(true)}>Audit Log</button>
            )}
            <button className="btn-header" onClick={() => setShowIngestWizard(true)}>+ Ingest</button>
          </span>
          <UserAvatar name={user?.displayName ?? null} email={user?.email ?? ''} photoUrl={user?.photoURL} size={26} />
          <span className="desktop-only" style={{ color: 'var(--color-ink)', fontWeight: 500 }}>{user?.displayName || user?.email}</span>
          <button className="btn-header" onClick={logout}>Sign out</button>
        </div>
      </div>

      {/* Content */}
      <div className="content-area" style={{ paddingTop: 'var(--space-4)', paddingBottom: 'var(--space-8)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
          <div style={{ flex: 1 }}>
            <SearchBar onSearch={handleSearch} initialQuery={searchQuery} />
          </div>
          <button
            className="btn btn-primary desktop-only"
            onClick={async () => {
              try {
                const { getRandomDocument } = await import('./api/client');
                const { id } = await getRandomDocument(production.id);
                setViewDocId(id);
              } catch (e: any) {
                showToast(
                  e?.message?.includes('404')
                    ? 'No documents in this production yet.'
                    : `Could not pick a random document: ${e?.message || 'unknown error'}`,
                  'error',
                );
              }
            }}
            style={{ flexShrink: 0, whiteSpace: 'nowrap' }}
          >
            I'm Feeling Lucky
          </button>
        </div>

        <TopicGroups
          clusters={clusters}
          activeClusterId={filterClusterId}
          onSelect={setFilterClusterId}
          onOpenAnalysis={() => setShowCorpusAnalysis(true)}
        />

        {/* My Review Batches */}
        {myBatches.length > 0 && (
          <div style={{ marginBottom: 'var(--space-4)' }}>
            <h3 style={{ fontSize: 'var(--text-sm)', fontWeight: 600, marginBottom: 'var(--space-2)', color: 'var(--color-neutral-600)' }}>
              My Review Batches
            </h3>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)' }}>
              {myBatches.map(b => (
                <div
                  key={b.id}
                  className="card"
                  style={{ padding: 'var(--space-3)', cursor: 'pointer', minWidth: 200 }}
                  onClick={() => setActiveBatchId(b.id)}
                >
                  <div style={{ fontWeight: 600, fontSize: 'var(--text-sm)' }}>{b.queue_name}</div>
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginTop: 'var(--space-1)' }}>
                    {b.reviewed_count}/{b.size} reviewed
                  </div>
                  <progress value={b.reviewed_count} max={b.size} style={{ width: '100%', marginTop: 'var(--space-1)' }} />
                </div>
              ))}
            </div>
          </div>
        )}

        {loading && (
          <div className="loading-center">
            <span className="spinner spinner-md" />
            <span>{hasSearched ? 'Searching…' : 'Loading documents…'}</span>
          </div>
        )}

        {/* Search results */}
        {hasSearched && !loading && (
          <>
            <div className="section-header">
              <h2 className="section-title">
                <button className="btn btn-ghost btn-sm" onClick={clearSearch} style={{ marginRight: 'var(--space-2)' }}>
                  ←
                </button>
                Search results
                <span className="section-count">{searchTotal}</span>
              </h2>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                <span style={{ fontSize: 'var(--text-xs)', color: 'rgba(44,62,107,0.5)', padding: '2px 8px', background: 'rgba(44,62,107,0.04)', borderRadius: 'var(--radius-sm)' }}>
                  {lastSearchMode === 'semantic' ? 'Semantic' : 'Full-text'}
                </span>
                <button
                  className="btn btn-ghost btn-xs"
                  onClick={() => handleSearch(searchQuery, undefined, lastSearchMode === 'semantic' ? 'fulltext' : 'semantic')}
                  style={{ fontSize: 'var(--text-xs)' }}
                >
                  Try {lastSearchMode === 'semantic' ? 'full-text' : 'semantic'}
                </button>
                <label htmlFor="search-filter-type" className="visually-hidden">Filter by file type</label>
                <select
                  id="search-filter-type"
                  className="input input-sm"
                  style={{ width: 'auto', minWidth: 140 }}
                  value={filterFileType}
                  onChange={e => setFilterFileType(e.target.value)}
                >
                  <option value="">All types</option>
                  <option value="images_only">Documents (images)</option>
                  <option value="video">Video</option>
                  <option value="audio">Audio</option>
                  <option value="pdf">PDF</option>
                  <option value="office">Office (Word/Excel/PPT)</option>
                  <option value="email">Email (.msg/.eml)</option>
                  <option value="image">Image (PNG/JPG)</option>
                  <option value="native">All native files</option>
                </select>
                <button className="btn btn-ghost btn-sm desktop-only" onClick={() => exportSearchCsv(searchQuery, production.id)}>
                  Export CSV
                </button>
              </div>
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
        {!hasSearched && !loading && (docTotal > 0 || filterTagId || filterFileType) && (
          <div>
            <div className="section-header">
              <h2 className="section-title">
                All Documents
                <span className="section-count">{docTotal}</span>
              </h2>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
                <label htmlFor="filter-tag" className="visually-hidden">Filter by tag</label>
                <select
                  id="filter-tag"
                  className="input input-sm"
                  style={{ width: 'auto', minWidth: 120 }}
                  value={filterTagId ?? ''}
                  onChange={e => { setFilterTagId(e.target.value ? Number(e.target.value) : null); setDocPage(1); }}
                >
                  <option value="">All tags</option>
                  {allTags.map(t => (
                    <option key={t.id} value={t.id}>{t.category}: {t.name}</option>
                  ))}
                </select>
                <label htmlFor="filter-type" className="visually-hidden">Filter by file type</label>
                <select
                  id="filter-type"
                  className="input input-sm"
                  style={{ width: 'auto', minWidth: 120 }}
                  value={filterFileType}
                  onChange={e => { setFilterFileType(e.target.value); setDocPage(1); }}
                >
                  <option value="">All types</option>
                  <option value="images_only">Documents (images)</option>
                  <option value="video">Video</option>
                  <option value="audio">Audio</option>
                  <option value="pdf">PDF</option>
                  <option value="office">Office (Word/Excel/PPT)</option>
                  <option value="email">Email (.msg/.eml)</option>
                  <option value="image">Image (PNG/JPG)</option>
                  <option value="native">All native files</option>
                </select>
                <label htmlFor="sort-by" className="visually-hidden">Sort order</label>
                <select
                  id="sort-by"
                  className="input input-sm"
                  style={{ width: 'auto', minWidth: 100 }}
                  value={sortBy}
                  onChange={e => { setSortBy(e.target.value); setDocPage(1); }}
                >
                  <option value="bates">Sort: Bates #</option>
                  <option value="recent">Sort: Recent</option>
                  <option value="size">Sort: Size</option>
                </select>
                {(filterTagId || filterFileType) && (
                  <button className="btn btn-ghost btn-xs" onClick={() => { setFilterTagId(null); setFilterFileType(''); setDocPage(1); }}>
                    Clear filters
                  </button>
                )}
                <button className="btn btn-ghost btn-sm desktop-only" onClick={() => exportDocsCsv(production.id)}>
                  Export CSV
                </button>
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
                      <th style={{ width: 80 }}>Type</th>
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
                          {d.processing_status !== 'complete' && (
                            <span className="badge badge-yellow" style={{ marginRight: 6, fontSize: 9 }}>Processing</span>
                          )}
                          <EditableTitle
                            docId={d.id}
                            title={d.title}
                            onUpdated={(newTitle) => {
                              setDocuments(prev => prev.map(doc => doc.id === d.id ? { ...doc, title: newTitle } : doc));
                            }}
                          />
                        </td>
                        <td className="meta-cell">
                          <span className={`badge badge-${
                            d.file_type === 'video' ? 'purple' :
                            d.file_type === 'audio' ? 'blue' :
                            d.file_type === 'email' ? 'yellow' :
                            d.file_type === 'pdf' ? 'red' :
                            d.file_type === 'spreadsheet' ? 'green' :
                            d.file_type === 'presentation' ? 'yellow' :
                            d.file_type === 'image' ? 'blue' :
                            'gray'
                          }`} style={{ fontSize: 10, textTransform: 'capitalize' }}>
                            {d.file_type === 'document' ? 'doc' : d.file_type}
                          </span>
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
                      {d.processing_status !== 'complete' ? (
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', background: 'var(--color-neutral-100)', color: 'var(--color-neutral-400)', fontSize: 'var(--text-xs)' }}>
                          <span className="spinner spinner-sm" style={{ marginRight: 6 }} />Processing
                        </div>
                      ) : d.page_count > 0 ? (
                        <AuthImage docId={d.id} pageNum={1} width={300} alt={d.bates_begin} loading="lazy" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                      ) : (
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', background: 'var(--color-neutral-100)', color: 'rgba(44,62,107,0.35)', fontSize: 28, flexDirection: 'column', gap: 4 }}>
                          {d.has_native ? '◉' : '○'}
                          <span style={{ fontSize: 'var(--text-xs)' }}>{d.title || 'Native'}</span>
                        </div>
                      )}
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

            {displayDocs.length === 0 && (
              <div style={{ padding: 'var(--space-8)', textAlign: 'center', color: 'var(--color-neutral-400)' }}>
                No documents match the current filters.
              </div>
            )}

            {/* Pagination */}
            <div className="pagination">
              <button
                className="btn btn-secondary btn-sm"
                disabled={docPage <= 1}
                onClick={() => loadDocuments(docPage - 1)}
              >
                ← Prev
              </button>
              <span className="page-info">
                Page {docPage} of {totalPages || 1}
              </span>
              <button
                className="btn btn-secondary btn-sm"
                disabled={docPage >= totalPages}
                onClick={() => loadDocuments(docPage + 1)}
              >
                Next →
              </button>
              <select
                value={perPage}
                onChange={e => { const v = Number(e.target.value); setPerPage(v); }}
                style={{
                  marginLeft: 'var(--space-3)', padding: '3px var(--space-2)', fontSize: 'var(--text-xs)',
                  border: '1px solid var(--color-neutral-300)', borderRadius: 'var(--radius-sm)',
                  color: 'var(--color-neutral-600)', background: 'white', cursor: 'pointer',
                }}
              >
                {[25, 50, 100, 200].map(n => (
                  <option key={n} value={n}>{n} per page</option>
                ))}
              </select>
            </div>
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
          productionName={production.name}
          onClose={() => setShowManageAccess(false)}
        />
      )}

      {/* Audit log modal */}
      {showAuditLog && (
        <AuditLog
          productionId={production.id}
          onClose={() => setShowAuditLog(false)}
        />
      )}

      {/* Ingest wizard modal */}
      {showIngestWizard && (
        <IngestWizard
          onClose={() => setShowIngestWizard(false)}
          onComplete={() => { setShowIngestWizard(false); onIngestComplete(); }}
        />
      )}

      {/* Queue manager modal */}
      {showQueueManager && (
        <QueueManager productionId={production.id} onClose={() => setShowQueueManager(false)} />
      )}

      {/* Dashboard modal */}
      {showDashboard && (
        <Dashboard productionId={production.id} onClose={() => setShowDashboard(false)} />
      )}

      {/* Footer */}
      <div style={{ textAlign: 'center', padding: 'var(--space-6) 0 var(--space-4)', fontSize: 11, color: 'rgba(44,62,107,0.3)' }}>
        Built by <a href="https://qndary.com" target="_blank" rel="noopener noreferrer" style={{ color: 'rgba(44,62,107,0.45)', textDecoration: 'none', fontWeight: 600 }}>QNDARY</a>
      </div>

      {/* Floating bulk action bar */}
      {selectedIds.size > 0 && (
        <div className="floating-bar">
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--text-xs)' }}>
            {selectedIds.size} selected
          </span>
          <div className="divider" style={{ background: 'rgba(255,255,255,0.15)', height: 20 }} />
          <button
            className="btn btn-sm btn-secondary"
            onClick={handleBulkDownload}
            disabled={bulkDownloading}
          >
            {bulkDownloading ? 'Preparing…' : 'Download'}
          </button>
          <button
            className="btn btn-sm btn-secondary"
            onClick={sendSelectionToAgent}
            style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          >
            <span className="ai-indicator" style={{ fontSize: 9, padding: '0 4px' }}>AI</span>
            Send to AI Agent
          </button>
          <div style={{ position: 'relative' }}>
            <button
              className="btn btn-sm btn-secondary"
              onClick={() => setShowBulkTagPicker(!showBulkTagPicker)}
              disabled={bulkTagging}
            >
              Tag
            </button>
            {showBulkTagPicker && (
              <div className="dropdown" style={{ bottom: '100%', left: 0, marginBottom: 8, minWidth: 260, maxHeight: 320, overflowY: 'auto' }}>
                <div style={{ padding: 'var(--space-2)', borderBottom: '1px solid var(--color-neutral-100)' }}>
                  <form
                    onSubmit={(e) => { e.preventDefault(); handleBulkCreateTag(); }}
                    style={{ display: 'flex', gap: 'var(--space-1)' }}
                  >
                    <label htmlFor="bulk-new-tag" className="visually-hidden">New tag name</label>
                    <input
                      id="bulk-new-tag"
                      className="input input-sm"
                      placeholder="+ New tag…"
                      value={newTagName}
                      onChange={e => setNewTagName(e.target.value)}
                      style={{ flex: 1 }}
                      disabled={bulkTagging}
                    />
                    <button
                      type="submit"
                      className="btn btn-primary btn-xs"
                      disabled={!newTagName.trim() || bulkTagging}
                    >
                      Add
                    </button>
                  </form>
                </div>
                {allTags.length === 0 ? (
                  <div className="empty-state" style={{ padding: 'var(--space-3)', fontSize: 'var(--text-xs)' }}>
                    No tags yet — type above to create one.
                  </div>
                ) : (
                  allTags.map(tag => (
                    <button
                      key={tag.id}
                      type="button"
                      className="dropdown-item"
                      onClick={() => handleBulkTag(tag.id)}
                      disabled={bulkTagging}
                      style={{ background: 'transparent', border: 'none', width: '100%', textAlign: 'left', cursor: 'pointer', font: 'inherit' }}
                    >
                      <span className={`badge ${COLOR_MAP[tag.color] || 'badge-gray'}`}>{tag.name}</span>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>
          <button
            className="btn btn-sm btn-ghost"
            onClick={() => { setSelectedIds(new Set()); setShowBulkTagPicker(false); setNewTagName(''); }}
          >
            Clear
          </button>
        </div>
      )}

      {/* Floating AI Agent launcher — hidden while the panel is open. */}
      {!chatOpen && (
        <button
          className="ai-agent-fab"
          onClick={() => setChatOpen(true)}
          aria-label="Open AI Agent"
          title="AI Agent"
        >
          <span className="ai-indicator" style={{ fontSize: 13, padding: '2px 7px', background: 'transparent', color: '#fff', boxShadow: 'none' }}>AI</span>
        </button>
      )}

      {/* AI Agent chat panel — kept mounted so the session conversation persists across open/close. */}
      <AIAgent
        open={chatOpen}
        onClose={() => setChatOpen(false)}
        attachedDocs={chatDocs}
        onRemoveDoc={(id) => setChatDocs(prev => prev.filter(d => d.id !== id))}
      />

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
      // Restore the active production from the URL if one was set
      // (so refresh lands you back on the same production), otherwise
      // auto-select when there's only one.
      const urlProd = getInitialUrlState().prod;
      const fromUrl = urlProd ? prods.find(p => String(p.id) === urlProd) : undefined;
      if (fromUrl) setActiveProduction(fromUrl);
      else if (prods.length === 1) setActiveProduction(prods[0]);
      else if (prods.length === 0) setActiveProduction(null);
    } catch (e: any) {
      showToast(`Could not load productions: ${e?.message || 'unknown error'}`, 'error');
    } finally {
      setProdLoading(false);
    }
  };

  useEffect(() => { loadProductions(); }, []);

  const handleIngestComplete = () => {
    setActiveProduction(null);
    loadProductions();
  };

  if (prodLoading) {
    return (
      <div className="loading-fullscreen">
        <span className="spinner" />
        <div>Loading productions…</div>
      </div>
    );
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
          onDeleted={loadProductions}
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
  const { user, loading, error } = useAuth();

  if (error) {
    return (
      <div style={{ padding: 40, fontFamily: 'sans-serif', textAlign: 'center', maxWidth: 480, margin: '80px auto' }}>
        <h2 style={{ marginBottom: 8, color: '#b91c1c' }}>Unable to connect</h2>
        <p style={{ color: '#6b7280', marginBottom: 16 }}>
          The app couldn't initialize authentication. This usually means Firebase
          is misconfigured or unreachable.
        </p>
        <pre style={{ background: '#f3f4f6', padding: 12, borderRadius: 8, fontSize: 13, textAlign: 'left', overflowX: 'auto', color: '#991b1b' }}>
          {error}
        </pre>
        <button
          onClick={() => window.location.reload()}
          style={{ marginTop: 20, padding: '8px 20px', cursor: 'pointer', border: '1px solid #d1d5db', borderRadius: 6, background: 'white' }}
        >
          Retry
        </button>
      </div>
    );
  }

  if (loading) return (
    <div className="loading-fullscreen">
      <span className="spinner" />
      <div>Signing you in…</div>
      <div className="loading-sub">Verifying your Vigilist session</div>
    </div>
  );
  if (!user) return <AuthPage />;
  return <AppRouter />;
}
