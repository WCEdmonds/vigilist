import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { bulkTag, createTag, designateSources, exportDocsCsv, exportSearchCsv, fetchBulkZip, getClusters, getEntitiesSummary, getMyBatches, getSourceParties, getTags, listDocuments, listProductions, searchDocuments } from './api/client';
import DocumentViewer from './components/DocumentViewer';
import AuthImage from './components/AuthImage';
import AuthPage from './components/AuthPage';
import EditableTitle from './components/EditableTitle';
import EntitiesView from './components/EntitiesView';
import EntityGraphView from './components/EntityGraphView';
import EntityTimelineView from './components/EntityTimelineView';
import IngestWizard from './components/IngestWizard';
import ProductionSetsPanel from './components/ProductionSetsPanel';
import AuditLog from './components/AuditLog';
import ManageAccess from './components/ManageAccess';
import ProductionSettings from './components/ProductionSettings';
import ReviewWorkspace from './components/ReviewWorkspace';
import BatchReview from './components/BatchReview';
import Dashboard from './components/Dashboard';
import AppHeader from './components/AppHeader';
import SearchResults from './components/SearchResults';
import ProductionBrief from './components/ProductionBrief';
import { ToastContainer, showToast } from './components/Toast';
import WelcomePage from './components/WelcomePage';
import ProductionPicker from './components/ProductionPicker';
import OnboardingGuide from './components/OnboardingGuide';
import ContextRail from './components/ContextRail';
import { AuthProvider, useAuth } from './hooks/useAuth';
import { getInitialUrlState, useSyncUrl } from './hooks/useUrlState';
import { useOnboarding } from './hooks/useOnboarding';
import { useChat } from './hooks/useChat';
import { SLIDES } from './onboarding/slides';
import { detectSearchMode, type SearchMode } from './utils/searchMode';
import type { ChipEntity, ClusterInfo, DocumentSummary, ProductionInfo, ReviewBatch, SearchResult, Tag } from './types';

const COLOR_MAP: Record<string, string> = {
  green: 'badge-green', red: 'badge-red', yellow: 'badge-yellow',
  purple: 'badge-purple', gray: 'badge-gray', blue: 'badge-blue',
};

type ViewMode = 'list' | 'grid';

interface HomeProps {
  production: ProductionInfo;
  productions: ProductionInfo[];
  onSelectProduction: (p: ProductionInfo) => void;
  onSwitchProduction: () => void;
  onIngestComplete: () => void;
  onOpenGuide: () => void;
}

function Home({ production, productions, onSelectProduction, onSwitchProduction, onIngestComplete, onOpenGuide }: HomeProps) {
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
  const [lastSearchMode, setLastSearchMode] = useState<SearchMode>('fulltext');
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [showBulkTagPicker, setShowBulkTagPicker] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>('list');
  const [filterTagId, setFilterTagId] = useState<number | null>(null);
  const [filterFileType, setFilterFileType] = useState<string>('');
  const [sortBy, setSortBy] = useState<string>('bates');
  const [filterAiDecision, setFilterAiDecision] = useState<string>('');
  const [filterSourceParty, setFilterSourceParty] = useState<string>('');
  const [sourceParties, setSourceParties] = useState<string[]>([]);
  const [undesignatedCount, setUndesignatedCount] = useState(0);
  const [workMode, setWorkModeState] = useState<'all' | 'incoming' | 'outgoing'>(
    () => (localStorage.getItem(`vigilist:mode:${production.id}`) as 'all' | 'incoming' | 'outgoing') || 'all',
  );
  const setWorkMode = (m: 'all' | 'incoming' | 'outgoing') => {
    setWorkModeState(m);
    localStorage.setItem(`vigilist:mode:${production.id}`, m);
  };
  const sourceTypeParam = workMode === 'incoming' ? 'received' : workMode === 'outgoing' ? 'collection' : undefined;

  const [showManageAccess, setShowManageAccess] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showAuditLog, setShowAuditLog] = useState(false);
  const [showIngestWizard, setShowIngestWizard] = useState(false);
  const [showDashboard, setShowDashboard] = useState(false);
  const [activeBatchId, setActiveBatchId] = useState<number | null>(
    initialUrl.batch ? Number(initialUrl.batch) : null,
  );
  const [myBatches, setMyBatches] = useState<ReviewBatch[]>([]);
  const [showReview, setShowReview] = useState(initialUrl.view === 'review' || initialUrl.view === 'ai');
  const [showEntities, setShowEntities] = useState(initialUrl.view === 'entities');
  const [showTimeline, setShowTimeline] = useState(initialUrl.view === 'timeline');
  const [showGraph, setShowGraph] = useState(initialUrl.view === 'graph');
  const [entityPanelId, setEntityPanelId] = useState<string | null>(initialUrl.entity ?? null);

  // Ambient entity chips shown on doc-list/search rows — a cache keyed by
  // document id, populated one batch call per newly-seen page of rows.
  const [entityChips, setEntityChips] = useState<Record<string, ChipEntity[]>>({});
  // Ids already requested (or in flight), so a re-render with the same page
  // doesn't refire the batch call. Not state — it never drives a render.
  const chipsFetched = useRef<Set<string>>(new Set());

  // Deep-link into an entity's profile panel from anywhere (chip clicks,
  // timeline participants, graph nodes, etc.) — closes other full-screen
  // views/the doc viewer and opens the Entities workspace with the panel seeded.
  const navigateToEntity = (id: string) => {
    setShowReview(false);
    setViewDocId(null);
    setEntityPanelId(id);
    setShowEntities(true);
  };

  // AI chat, docked in the context rail (session-only conversation). The
  // production id lets doc-less questions be grounded in the production
  // ("ask the production") via server-side retrieval.
  const chat = useChat(production.id);
  const [railCollapsed, setRailCollapsed] = useState(() => {
    try {
      // Below 1025px the rail is an overlay drawer — starting it open would
      // hide the whole page, so the stored desktop preference doesn't apply.
      if (window.innerWidth < 1025) return true;
      const stored = window.localStorage.getItem('vigilist.rail.collapsed');
      if (stored !== null) return stored === '1';
      return false;
    } catch { return false; }
  });
  const toggleRail = useCallback(() => {
    setRailCollapsed(prev => {
      const next = !prev;
      try { window.localStorage.setItem('vigilist.rail.collapsed', next ? '1' : '0'); } catch { /* storage unavailable */ }
      return next;
    });
  }, []);
  const [askFocusToken, setAskFocusToken] = useState(0);
  const focusChat = useCallback(() => {
    setRailCollapsed(prev => {
      if (prev) {
        try { window.localStorage.setItem('vigilist.rail.collapsed', '0'); } catch { /* storage unavailable */ }
      }
      return false;
    });
    setAskFocusToken(t => t + 1);
  }, []);

  const handleAsk = useCallback((question: string) => {
    focusChat();
    chat.send(question);
  }, [chat, focusChat]);

  // Mirror the key bits of state back into the URL so a refresh lands
  // the user on the same page (doc viewer, batch review, search, etc.).
  useSyncUrl({
    prod: String(production.id),
    doc: viewDocId ?? undefined,
    q: hasSearched ? searchQuery || undefined : undefined,
    batch: activeBatchId ? String(activeBatchId) : undefined,
    view: showReview ? 'review' : showEntities ? 'entities' : showTimeline ? 'timeline' : showGraph ? 'graph' : undefined,
    entity: (showEntities || showTimeline || showGraph) && entityPanelId ? entityPanelId : undefined,
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

  const refreshClusters = useCallback(() => {
    getClusters(production.id).then(setClusters).catch(e => console.warn('getClusters failed:', e));
  }, [production.id]);

  useEffect(() => {
    loadDocuments();
    getTags().then(setAllTags).catch(e => console.warn('getTags failed:', e));
    getMyBatches(production.id).then(setMyBatches).catch(e => console.warn('getMyBatches failed:', e));
    getSourceParties(production.id).then(r => { setSourceParties(r.source_parties); setUndesignatedCount(r.undesignated); }).catch(e => console.warn('getSourceParties failed:', e));
    refreshClusters();
  }, [production.id, perPage, filterTagId, filterFileType, sortBy, filterClusterId, refreshClusters, filterAiDecision, filterSourceParty, workMode]);

  const loadDocuments = async (page = 1) => {
    setLoading(true);
    try {
      const res = await listDocuments(page, perPage, production.id, filterTagId ?? undefined, filterFileType || undefined, sortBy, filterClusterId ?? undefined, filterAiDecision || undefined, filterSourceParty || undefined, sourceTypeParam);
      setDocuments(res.documents);
      setDocTotal(res.total);
      setDocPage(page);
    } catch (e) {
      showToast(`Could not load documents: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  const [lastMetadata, setLastMetadata] = useState<Record<string, string> | undefined>(undefined);

  const handleSearch = async (query: string, metadata?: Record<string, string>, forceMode?: SearchMode) => {
    // A query that looks like a Bates number jumps straight to that document
    // (tolerant matching server-side); anything unresolved falls through to a
    // normal search.
    const trimmed = query.trim();
    if (!metadata && /^[A-Za-z]{2,}[\s\-_.]*\d{3,}$/.test(trimmed)) {
      try {
        const { getByBates } = await import('./api/client');
        const found = await getByBates(trimmed, production.id);
        setViewDocId(found.id);
        return;
      } catch { /* not a Bates number in this production — search normally */ }
    }

    setLoading(true);
    setSearchQuery(query);
    setHasSearched(true);
    setSelectedIds(new Set());
    setLastMetadata(metadata);

    const mode = forceMode ?? detectSearchMode(query);
    setLastSearchMode(mode);

    try {
      const res = await searchDocuments(
        query, 1, perPage, 'relevance', production.id,
        metadata, mode, filterFileType || undefined,
        filterSourceParty || undefined, sourceTypeParam,
      );
      setSearchResults(res.results);
      setSearchTotal(res.total);
    } catch (e) {
      showToast(`Search failed: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  // Re-run the active search whenever the file-type filter changes.
  useEffect(() => {
    if (hasSearched) handleSearch(searchQuery, lastMetadata, lastSearchMode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterFileType, filterSourceParty, workMode]);

  // Ambient entity chips: batch-fetch summaries for whichever page of rows
  // is currently on screen (doc list or search results), one call per newly
  // seen id set. Mirrors ProductionBrief's expand-effect ref-guard pattern.
  useEffect(() => {
    const rowIds = hasSearched ? searchResults.map(r => r.id) : documents.map(d => d.id);
    const toFetch = rowIds.filter(id => !chipsFetched.current.has(id));
    if (toFetch.length === 0) return;
    toFetch.forEach(id => chipsFetched.current.add(id));
    getEntitiesSummary(toFetch)
      .then(r => setEntityChips(prev => ({ ...prev, ...r.summaries })))
      .catch(e => console.warn('getEntitiesSummary failed:', e));
  }, [hasSearched, documents, searchResults]);

  const handleDesignateAll = async (sourceType: 'collection' | 'received') => {
    try {
      const r = await designateSources(production.id, sourceType);
      showToast(`Marked ${r.updated} document${r.updated === 1 ? '' : 's'} as ${sourceType === 'received' ? 'received' : 'our collection'}`, 'success');
      getSourceParties(production.id).then(x => { setSourceParties(x.source_parties); setUndesignatedCount(x.undesignated); }).catch(() => {});
      if (hasSearched) handleSearch(searchQuery, lastMetadata, lastSearchMode);
      else loadDocuments(docPage);
    } catch (e) {
      showToast(`Could not designate: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
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
      if (hasSearched) handleSearch(searchQuery, lastMetadata, lastSearchMode);
      else loadDocuments(docPage);
    } catch (e) {
      showToast(`Could not apply tag: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
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
      if (hasSearched) handleSearch(searchQuery, lastMetadata, lastSearchMode);
      else loadDocuments(docPage);
    } catch (e) {
      showToast(`Could not create tag: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
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
    } catch (e) {
      showToast(`Could not build download: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
    } finally {
      setBulkDownloading(false);
    }
  };

  const handleRandomDoc = async () => {
    try {
      const { getRandomDocument } = await import('./api/client');
      const { id } = await getRandomDocument(production.id);
      setViewDocId(id);
    } catch (e) {
      showToast(
        e instanceof Error && e.message.includes('404')
          ? 'No documents in this production yet.'
          : `Could not pick a random document: ${e instanceof Error ? e.message : 'unknown error'}`,
        'error',
      );
    }
  };

  const themeIndexById = useMemo(() => {
    const m = new Map<number, number>();
    clusters.forEach((c, i) => m.set(c.id, (i % 8) + 1));
    return m;
  }, [clusters]);

  const themeChip = (d: DocumentSummary) => {
    // No label means the AI judged the cluster to have no genuine common
    // thread — showing a placeholder badge would imply a theme that isn't
    // there, so these rows get no badge. (The brief's chips still expose the
    // cluster as "Cluster N" for filtering.)
    if (d.cluster_id == null || !themeIndexById.has(d.cluster_id) || !d.cluster_label) return null;
    const active = filterClusterId === d.cluster_id;
    return (
      <button
        type="button"
        className={`doc-theme-chip${active ? ' is-active' : ''}`}
        style={{ background: `var(--theme-${themeIndexById.get(d.cluster_id)})` }}
        onClick={e => { e.stopPropagation(); setFilterClusterId(active ? null : d.cluster_id!); }}
        title={d.cluster_label}
      >
        {d.cluster_label}
      </button>
    );
  };

  const aiMarker = (d: DocumentSummary) => {
    if (!d.ai_decision || d.ai_decided) return null;
    const label = d.ai_decision.replace(/_/g, ' ');
    const color =
      d.ai_decision === 'key_document' ? 'var(--color-primary-400)' :
      d.ai_decision === 'relevant' ? 'var(--color-success)' :
      d.ai_decision === 'needs_review' ? 'var(--color-warning)' :
      'var(--color-neutral-400)';
    return (
      <span className="ai-marker" style={{ color }}>
        <span className="ai-marker-star">✦</span> {label} {d.ai_confidence ?? 0}%
      </span>
    );
  };

  // Ambient entity chip — clicking retargets the (possibly already-open)
  // Entities panel via navigateToEntity rather than bubbling into the row's
  // own click handler (which opens the document).
  const entityChip = (c: ChipEntity) => (
    <button key={c.entity_id} className="badge badge-gray" style={{ cursor: 'pointer' }}
            onClick={ev => { ev.stopPropagation(); navigateToEntity(c.entity_id); }}>
      <span className={`entity-dot entity-${c.entity_type}`} style={{ marginRight: 3 }}>●</span>
      {c.canonical_name}
    </button>
  );

  // Re-fetch the doc list (or re-run the active search) after leaving the
  // review workspace, since markers/tags may have changed there. Called from
  // event handlers only — never from a render-time effect.
  const refreshList = () => {
    if (hasSearched) handleSearch(searchQuery, lastMetadata, lastSearchMode);
    else loadDocuments(docPage);
  };

  // Review workspace full-screen mode (AI lane + human queue/batch lane)
  if (showReview) {
    return (
      <ReviewWorkspace
        production={production}
        onViewDocument={(id) => { setShowReview(false); setViewDocId(id); refreshList(); }}
        onBack={() => { setShowReview(false); refreshList(); }}
      />
    );
  }

  // Entities view full-screen mode (key players + merge suggestion queue)
  // Leaving via Back has no deep-link intent, so the panel id is cleared
  // before the view flag flips — otherwise a stale id from this visit would
  // auto-open a panel the next time a full-screen view mounts. (This is
  // defense-in-depth: exclusivity between full-screen views is already
  // guaranteed structurally by the if-return branch order below.)
  if (showEntities) {
    return (
      <EntitiesView
        productionId={production.id}
        onViewDocument={(id) => { setShowEntities(false); setViewDocId(id); refreshList(); }}
        onBack={() => { setEntityPanelId(null); setShowEntities(false); refreshList(); }}
        openEntityId={entityPanelId}
        onOpenEntityChange={setEntityPanelId}
      />
    );
  }

  // Timeline view full-screen mode (chronological case events)
  // Same rationale as the Entities onBack above — Back has no deep-link
  // intent, so clear the stale panel id first.
  if (showTimeline) {
    return (
      <EntityTimelineView
        productionId={production.id}
        onViewDocument={(id) => { setShowTimeline(false); setViewDocId(id); refreshList(); }}
        onBack={() => { setEntityPanelId(null); setShowTimeline(false); refreshList(); }}
        openEntityId={entityPanelId}
        onOpenEntityChange={setEntityPanelId}
      />
    );
  }

  // Graph view full-screen mode (relationship graph)
  // Same rationale as the Entities/Timeline onBack above — Back has no
  // deep-link intent, so clear the stale panel id first.
  if (showGraph) {
    return (
      <EntityGraphView
        productionId={production.id}
        onViewDocument={(id) => { setShowGraph(false); setViewDocId(id); refreshList(); }}
        onBack={() => { setEntityPanelId(null); setShowGraph(false); refreshList(); }}
        openEntityId={entityPanelId}
        onOpenEntityChange={setEntityPanelId}
      />
    );
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
      <AppHeader
        production={production}
        productions={productions}
        onSelectProduction={onSelectProduction}
        onShowAllProductions={onSwitchProduction}
        onSearch={handleSearch}
        onLogoClick={clearSearch}
        initialQuery={searchQuery}
        onAsk={handleAsk}
        /* entityPanelId is cleared before each target view flag flips —
           these are plain nav clicks, not deep links, so no panel should
           carry over from whatever view was open before. Defense-in-depth
           only: view exclusivity is already structural (branch order above). */
        onOpenReview={() => { setEntityPanelId(null); setShowReview(true); }}
        onOpenEntities={() => { setEntityPanelId(null); setShowTimeline(false); setShowGraph(false); setShowEntities(true); }}
        onOpenTimeline={() => { setEntityPanelId(null); setShowEntities(false); setShowGraph(false); setShowTimeline(true); }}
        onOpenGraph={() => { setEntityPanelId(null); setShowEntities(false); setShowTimeline(false); setShowGraph(true); }}
        onOpenDashboard={() => { setEntityPanelId(null); setShowDashboard(true); }}
        onOpenShare={production.is_owner ? () => setShowManageAccess(true) : undefined}
        onOpenSettings={production.is_owner ? () => setShowSettings(true) : undefined}
        onOpenAudit={production.is_owner ? () => setShowAuditLog(true) : undefined}
        onOpenIngest={() => setShowIngestWizard(true)}
        onOpenGuide={onOpenGuide}
        onRandomDoc={handleRandomDoc}
      />

      {/* Content */}
      <div className={`home-shell${railCollapsed ? '' : ' rail-open'}`}>
        <div className="content-area" style={{ paddingTop: 'var(--space-4)', paddingBottom: 'var(--space-8)' }}>
        {/* Workspace mode: presets the source_type filter; never hides anything a direct link reaches */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 4, marginBottom: 'var(--space-2)' }}>
          {(['all', 'incoming', 'outgoing'] as const).map(m => (
            <button
              key={m}
              type="button"
              className={workMode === m ? 'btn btn-primary btn-xs' : 'btn btn-ghost btn-xs'}
              onClick={() => setWorkMode(m)}
              title={m === 'incoming' ? 'Focus on received productions' : m === 'outgoing' ? 'Focus on our collection' : 'All documents'}
            >
              {m === 'all' ? 'All' : m === 'incoming' ? 'Incoming' : 'Outgoing'}
            </button>
          ))}
        </div>
        {undesignatedCount > 0 && workMode !== 'all' && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap',
            fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)',
            background: 'rgba(154,103,0,0.06)', border: '1px solid rgba(154,103,0,0.25)',
            borderRadius: 'var(--radius-md)', padding: 'var(--space-2) var(--space-3)',
            marginBottom: 'var(--space-2)',
          }}>
            <span>
              <strong>{undesignatedCount}</strong> document{undesignatedCount === 1 ? '' : 's'} in this matter
              {' '}ha{undesignatedCount === 1 ? 's' : 've'} no source designation (ingested before sources existed).
              Mark them all as:
            </span>
            <button className="btn btn-secondary btn-xs" onClick={() => handleDesignateAll('received')}>Received production</button>
            <button className="btn btn-secondary btn-xs" onClick={() => handleDesignateAll('collection')}>Our collection</button>
          </div>
        )}
        {workMode === 'outgoing' && (
          <ProductionSetsPanel
            productionId={production.id}
            tags={allTags}
            selectedIds={selectedIds}
            onOpenDoc={setViewDocId}
          />
        )}
        <ProductionBrief
          production={production}
          clusters={clusters}
          activeClusterId={filterClusterId}
          onSelectCluster={setFilterClusterId}
          onViewDocument={setViewDocId}
          onPipelineSettled={refreshClusters}
          onOpenEntity={navigateToEntity}
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
                  onClick={() => handleSearch(searchQuery, lastMetadata, lastSearchMode === 'semantic' ? 'fulltext' : 'semantic')}
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
                <select
                  aria-label="Filter by source"
                  className="input input-sm"
                  style={{ width: 'auto', minWidth: 120 }}
                  value={filterSourceParty}
                  onChange={e => { setFilterSourceParty(e.target.value); setDocPage(1); }}
                >
                  <option value="">All sources</option>
                  {sourceParties.map(sp => (
                    <option key={sp} value={sp}>{sp}</option>
                  ))}
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
                entityChips={entityChips}
                onOpenEntity={navigateToEntity}
              />
            </div>
          </>
        )}

        {/* Document browse list */}
        {!hasSearched && !loading && (docTotal > 0 || filterTagId || filterFileType || filterAiDecision || filterSourceParty || workMode !== 'all') && (
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
                <select
                  aria-label="Filter by source"
                  className="input input-sm"
                  style={{ width: 'auto', minWidth: 120 }}
                  value={filterSourceParty}
                  onChange={e => { setFilterSourceParty(e.target.value); setDocPage(1); }}
                >
                  <option value="">All sources</option>
                  {sourceParties.map(sp => (
                    <option key={sp} value={sp}>{sp}</option>
                  ))}
                </select>
                <label htmlFor="filter-ai" className="visually-hidden">Filter by AI decision</label>
                <select
                  id="filter-ai"
                  className="input input-sm"
                  style={{ width: 'auto', minWidth: 120 }}
                  value={filterAiDecision}
                  onChange={e => { setFilterAiDecision(e.target.value); setDocPage(1); }}
                >
                  <option value="">AI: All</option>
                  <option value="relevant">AI: Relevant</option>
                  <option value="key_document">AI: Key document</option>
                  <option value="not_relevant">AI: Not relevant</option>
                  <option value="needs_review">AI: Needs review</option>
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
                {(filterTagId || filterFileType || filterAiDecision) && (
                  <button className="btn btn-ghost btn-xs" onClick={() => { setFilterTagId(null); setFilterFileType(''); setFilterAiDecision(''); setDocPage(1); }}>
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
              <div className="card" style={{ overflowX: 'auto', overflowY: 'hidden' }}>
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
                      <th>Theme</th>
                      <th>AI</th>
                      <th>People/Orgs</th>
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
                            {d.file_type === 'document' ? 'DOC' : d.file_type === 'pdf' ? 'PDF' : d.file_type}
                          </span>
                        </td>
                        <td className="meta-cell">{themeChip(d)}</td>
                        <td className="meta-cell">{aiMarker(d)}</td>
                        <td className="meta-cell">{(entityChips[d.id] || []).slice(0, 3).map(entityChip)}</td>
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
                        {themeChip(d)}
                        {aiMarker(d)}
                        {(entityChips[d.id] || []).slice(0, 3).map(entityChip)}
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
        <ContextRail
          production={production}
          chat={chat}
          collapsed={railCollapsed}
          onToggleCollapsed={toggleRail}
          autoFocusToken={askFocusToken}
          selectedIds={selectedIds}
          documents={documents}
          searchResults={searchResults}
          onViewDocument={setViewDocId}
          onSimilarResults={(label, results) => {
            setSearchQuery(label);
            setHasSearched(true);
            setSearchResults(results);
            setSearchTotal(results.length);
          }}
          onAttached={focusChat}
        />
      </div>

      {/* Manage access modal */}
      {showManageAccess && (
        <ManageAccess
          productionId={production.id}
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

      {showSettings && (
        <ProductionSettings
          production={production}
          onClose={() => setShowSettings(false)}
          onSaved={(updated) => { onSelectProduction(updated); setShowSettings(false); }}
        />
      )}

      {/* Ingest wizard modal */}
      {showIngestWizard && (
        <IngestWizard
          existingProduction={{ id: production.id, name: production.name }}
          onClose={() => setShowIngestWizard(false)}
          onComplete={() => { setShowIngestWizard(false); onIngestComplete(); }}
        />
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
    } catch (e) {
      showToast(`Could not load productions: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
    } finally {
      setProdLoading(false);
    }
  };

  useEffect(() => { loadProductions(); }, []);

  const handleIngestComplete = () => {
    setActiveProduction(null);
    loadProductions();
  };

  const { user } = useAuth();
  const { open: guideOpen, close: closeGuide, dismissForever, reopen: openGuide } = useOnboarding(user?.uid);

  // Someone with zero productions is about to ingest and become an owner —
  // they are exactly who needs the owner slide.
  const showOwnerSlides = productions.length === 0 || productions.some(p => p.is_owner);
  const slides = useMemo(
    () => SLIDES.filter(s => showOwnerSlides || !s.ownerOnly),
    [showOwnerSlides],
  );

  // Don't show the guide over a loading spinner.
  if (prodLoading) {
    return (
      <div className="loading-fullscreen">
        <span className="spinner" />
        <div>Loading productions…</div>
      </div>
    );
  }

  let content: ReactNode;
  if (productions.length === 0) {
    content = <WelcomePage onIngest={() => setShowIngestWizard(true)} />;
  } else if (!activeProduction) {
    content = (
      <ProductionPicker
        productions={productions}
        onSelect={setActiveProduction}
        onIngest={() => setShowIngestWizard(true)}
        onDeleted={loadProductions}
      />
    );
  } else {
    content = (
      <Home
        key={activeProduction.id}
        production={activeProduction}
        productions={productions}
        onSelectProduction={setActiveProduction}
        onSwitchProduction={() => setActiveProduction(null)}
        onIngestComplete={handleIngestComplete}
        onOpenGuide={openGuide}
      />
    );
  }

  return (
    <>
      {content}
      {/* Only the WelcomePage and ProductionPicker branches ever rendered this
          wizard. `Home` renders its own from its own state, so guarding on
          !activeProduction keeps a minimized in-flight ingest from surviving into
          Home and doubling up with Home's instance. */}
      {showIngestWizard && !activeProduction && (
        <IngestWizard onClose={() => setShowIngestWizard(false)} onComplete={handleIngestComplete} />
      )}
      {guideOpen && (
        <OnboardingGuide
          slides={slides}
          onClose={closeGuide}
          onDismissForever={dismissForever}
        />
      )}
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
  return <AppRouter key={user.uid} />;
}
