import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  acceptMergeSuggestion, autoResolveTypos, deleteEntity, getEntityConnections, getEntityMentions,
  listEntities, listMergeSuggestions, mergeEntities, rejectMergeSuggestion, renameEntity,
  triggerEntityExtraction,
} from '../api/client';
import { entityDisplayName } from '../utils/entityDisplay';
import type { EntityConnections, EntityListItem, MergeSuggestion } from '../types';
import EntityPanel from './EntityPanel';
import { showToast } from './Toast';

/* ────────────────────────────────────────────────────────────────────────
   The cast page. Billing is the information architecture: prominence in
   the record (mentions × documents) sets each entity's tier the way
   significance sets a timeline event's — principals get substantial cards,
   the supporting cast gets compact rows, one-off names collapse to an
   index. See styles/cast.css for the visual rationale.
   ──────────────────────────────────────────────────────────────────────── */

const PER_PAGE = 100; // the endpoint's per_page cap
const PRINCIPAL_CAP = 8;
const TOP_CONNECTIONS = 3;

type Tier = 'principal' | 'supporting' | 'mentioned';

/** Billing. Absolute floors keep small matters honest (nobody is a
 * "principal" on four mentions unless they lead the record), the relative
 * term keeps large ones honest (3% of the lead's mentions is not a lead). */
function tierOf(e: EntityListItem, topMentions: number): Tier {
  if (e.mention_count >= Math.max(6, topMentions * 0.3) && e.document_count >= 2) return 'principal';
  if (e.mention_count >= 3) return 'supporting';
  return 'mentioned';
}

const PERSON_SUFFIX = /^(jr|sr|ii|iii|iv|esq)\.?$/i;
const TILE_STOPWORD = /^(the|of|and|for|a|an|&)$/i;

/** Monogram for the letterpress tile: first + last initial for a person
 * (suffixes dropped), first two significant initials for an org. */
function initialsOf(display: string, type: 'person' | 'org'): string {
  let tokens = display.replace(/[^A-Za-z0-9' -]/g, ' ').split(/[\s-]+/)
    .filter(t => t.length > 0 && !TILE_STOPWORD.test(t));
  if (type === 'person') {
    while (tokens.length > 1 && PERSON_SUFFIX.test(tokens[tokens.length - 1])) tokens = tokens.slice(0, -1);
    if (tokens.length === 0) return '·';
    const first = tokens[0][0];
    return (tokens.length > 1 ? first + tokens[tokens.length - 1][0] : first).toUpperCase();
  }
  if (tokens.length === 0) return '·';
  return tokens.slice(0, 2).map(t => t[0]).join('').toUpperCase();
}

function monthYear(iso: string): string {
  return new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
}

function spanLabel(first: string, last: string): string {
  const a = monthYear(first);
  const b = monthYear(last);
  return a === b ? a : `${a} – ${b}`;
}

interface TimeScale { min: number; max: number; }

/** Segment of the shared presence rail: this entity's tenure over the
 * matter's full dated span. A floor width keeps one-day spans visible. */
function railSegment(e: EntityListItem, scale: TimeScale): { left: string; width: string } | null {
  if (!e.first_seen || !e.last_seen) return null;
  const range = scale.max - scale.min;
  if (range <= 0) return { left: '0%', width: '100%' };
  const left = Math.max(0, ((Date.parse(e.first_seen) - scale.min) / range) * 100);
  const width = Math.max(((Date.parse(e.last_seen) - Date.parse(e.first_seen)) / range) * 100, 1.5);
  return { left: `${left}%`, width: `${Math.min(width, 100 - left)}%` };
}

interface ConnBrief {
  entityId: string;
  name: string;
  rel: string;
  entityType: 'person' | 'org';
}

/** Top counterparts for a principal card: stated relationships first
 * (weighted by how many documents state them), co-occurrence as filler. */
function briefsOf(c: EntityConnections): ConnBrief[] {
  const byId = new Map<string, { first: ConnBrief; n: number }>();
  for (const s of c.stated) {
    const cur = byId.get(s.entity_id);
    if (cur) { cur.n += 1; continue; }
    byId.set(s.entity_id, {
      n: 1,
      first: {
        entityId: s.entity_id,
        name: entityDisplayName(s.canonical_name, s.entity_type),
        rel: (s.relationship_type || 'linked').replace(/_/g, ' '),
        entityType: s.entity_type,
      },
    });
  }
  const out = [...byId.values()].sort((a, b) => b.n - a.n).slice(0, TOP_CONNECTIONS).map(v => v.first);
  for (const co of c.cooccurrence) {
    if (out.length >= TOP_CONNECTIONS) break;
    if (byId.has(co.entity_id)) continue;
    out.push({
      entityId: co.entity_id,
      name: entityDisplayName(co.canonical_name, co.entity_type),
      rel: `${co.shared_doc_count ?? 0} shared docs`,
      entityType: co.entity_type,
    });
  }
  return out;
}

interface CtxRow {
  bates: string;
  text: string;
  surface: string;
}

/** Snippet with the entity's surface text marker-highlighted. */
function CtxSnippet({ row }: { row: CtxRow }) {
  const idx = row.surface ? row.text.toLowerCase().indexOf(row.surface.toLowerCase()) : -1;
  return (
    <div className="merge-ctx-row">
      <span className="merge-ctx-bates">{row.bates}</span>
      {idx === -1 ? <>…{row.text}…</> : (
        <>
          …{row.text.slice(0, idx)}
          <span className="marker-hl">{row.text.slice(idx, idx + row.surface.length)}</span>
          {row.text.slice(idx + row.surface.length)}…
        </>
      )}
    </div>
  );
}

// The keeper (merge winner) for a suggestion, defaulting to the more-frequent
// entity; an explicit choice in `winners` overrides the default.
function defaultKeeperId(s: MergeSuggestion): string {
  return s.entity_a.mention_count >= s.entity_b.mention_count ? s.entity_a.id : s.entity_b.id;
}

function mergeRows(prev: EntityListItem[], incoming: EntityListItem[]): EntityListItem[] {
  const seen = new Set(prev.map(e => e.id));
  return [...prev, ...incoming.filter(e => !seen.has(e.id))];
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

interface Props {
  productionId: number;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
  openEntityId?: string | null;
  onOpenEntityChange?: (id: string | null) => void;
}

const TYPE_LENSES: { value: string; label: string }[] = [
  { value: '', label: 'Everyone' },
  { value: 'person', label: 'People' },
  { value: 'org', label: 'Organizations' },
];

export default function EntitiesView({ productionId, onViewDocument, onBack, openEntityId, onOpenEntityChange }: Props) {
  // ── Cast data (timeline's paging pattern: `loading` derives from the
  // highest settled page, so no effect ever flips a loading flag) ──
  const [entities, setEntities] = useState<EntityListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [settledPage, setSettledPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  // ── Lenses ──
  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState(''); // debounced, applied form
  const [typeFilter, setTypeFilter] = useState('');

  // ── Review docket (merge suggestions) ──
  const [suggestions, setSuggestions] = useState<MergeSuggestion[]>([]);
  const [suggTick, setSuggTick] = useState(0);
  const [docketOpen, setDocketOpen] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [winners, setWinners] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [typoMsg, setTypoMsg] = useState<string | null>(null);

  // Hover context for merge review: "Isabella" vs "Isabel" is undecidable
  // without seeing each name in its documents. Snippets fetch lazily on
  // first hover and cache per entity.
  const [ctxCache, setCtxCache] = useState<Record<string, CtxRow[] | 'loading'>>({});
  const [hoverCtxId, setHoverCtxId] = useState<string | null>(null);

  // ── Extraction ──
  const [extracting, setExtracting] = useState(false);
  const [extractMsg, setExtractMsg] = useState<string | null>(null);

  // ── Inline curation (rename / delete, the chronology's editing idiom) ──
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState('');
  const [savingName, setSavingName] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deleteBusyId, setDeleteBusyId] = useState<string | null>(null);

  // ── Principal extras: top connections, fetched once per principal ──
  const [connMap, setConnMap] = useState<Record<string, ConnBrief[]>>({});
  const requestedConnsRef = useRef<Set<string>>(new Set());

  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const docketRef = useRef<HTMLElement>(null);

  const loading = settledPage < page;
  const filtered = Boolean(query || typeFilter);

  const openEntity = (id: string | null) => { onOpenEntityChange?.(id); };

  // ── Paging reset. `clearRows` only for lens changes (stale rows would
  // mismatch the new filter); refreshes keep rows up until page 1 lands. ──
  const resetPaging = useCallback((clearRows: boolean) => {
    if (clearRows) setEntities([]);
    setPage(1);
    setSettledPage(0);
    setHasMore(false);
    setLoadError(null);
    setEditingId(null);
    setConfirmingId(null);
    if (clearRows) scrollRef.current?.scrollTo({ top: 0 });
  }, []);

  // Full refresh after anything that rewrites the graph (merge, typo sweep,
  // extraction progress): cast, docket, and the per-principal connection
  // cache all go back to the server.
  const refreshAll = useCallback(() => {
    requestedConnsRef.current = new Set();
    setConnMap({});
    resetPaging(false);
    setRefreshTick(t => t + 1);
    setSuggTick(t => t + 1);
  }, [resetPaging]);

  // ── Load a page of the cast ──
  useEffect(() => {
    let cancelled = false;
    listEntities(productionId, query || undefined, typeFilter || undefined, page, PER_PAGE)
      .then(r => {
        if (cancelled) return;
        setEntities(prev => (page === 1 ? r.entities : mergeRows(prev, r.entities)));
        setTotal(r.total);
        setHasMore(r.entities.length === PER_PAGE && (page - 1) * PER_PAGE + r.entities.length < r.total);
        setLoadError(null);
        setSettledPage(page);
      })
      .catch(err => {
        if (cancelled) return;
        setLoadError(errText(err));
        setHasMore(false);
        setSettledPage(page);
      });
    return () => { cancelled = true; };
  }, [productionId, query, typeFilter, page, refreshTick]);

  // ── Load the docket ──
  useEffect(() => {
    let cancelled = false;
    listMergeSuggestions(productionId)
      .then(s => { if (!cancelled) setSuggestions(s); })
      .catch(e => console.warn('listMergeSuggestions failed:', e));
    return () => { cancelled = true; };
  }, [productionId, suggTick]);

  // ── Debounced search → applied query ──
  useEffect(() => {
    const q = searchInput.trim();
    if (q === query) return;
    const timer = setTimeout(() => {
      setQuery(q);
      resetPaging(true);
    }, 250);
    return () => clearTimeout(timer);
  }, [searchInput, query, resetPaging]);

  // ── Infinite scroll (same guard as the timeline: the observer only exists
  // while a next page genuinely exists and nothing is in flight) ──
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || loading || !hasMore || loadError) return;
    let fired = false;
    const io = new IntersectionObserver(entries => {
      if (fired || !entries.some(en => en.isIntersecting)) return;
      fired = true;
      io.disconnect();
      setPage(p => p + 1);
    }, { root: scrollRef.current, rootMargin: '500px 0px' });
    io.observe(sentinel);
    return () => io.disconnect();
  }, [loading, hasMore, loadError]);

  // ── While a backfill runs, poll so names appear as documents process ──
  useEffect(() => {
    if (!extracting) return;
    const timer = setInterval(refreshAll, 15000);
    return () => clearInterval(timer);
  }, [extracting, refreshAll]);

  // ── Billing ──
  const tiers = useMemo(() => {
    const principals: EntityListItem[] = [];
    const supporting: EntityListItem[] = [];
    const mentioned: EntityListItem[] = [];
    const top = entities[0]?.mention_count ?? 0;
    for (const e of entities) {
      const t = tierOf(e, top);
      if (t === 'principal' && principals.length < PRINCIPAL_CAP) principals.push(e);
      else if (t === 'mentioned') mentioned.push(e);
      else supporting.push(e);
    }
    return { principals, supporting, mentioned };
  }, [entities]);

  // Shared time scale for the presence rails: the matter's full dated span,
  // as established by every loaded entity's first/last dated event.
  const scale = useMemo<TimeScale | null>(() => {
    let min = Infinity;
    let max = -Infinity;
    for (const e of entities) {
      if (e.first_seen) min = Math.min(min, Date.parse(e.first_seen));
      if (e.last_seen) max = Math.max(max, Date.parse(e.last_seen));
    }
    return min <= max ? { min, max } : null;
  }, [entities]);

  // Entities the docket has under suggestion, for the marker ticks.
  const dupIds = useMemo(() => {
    const s = new Set<string>();
    for (const g of suggestions) { s.add(g.entity_a.id); s.add(g.entity_b.id); }
    return s;
  }, [suggestions]);

  // ── Fetch top connections for principals, once each. The ref set dedupes
  // across renders; state lands via .then, never synchronously. ──
  useEffect(() => {
    for (const e of tiers.principals) {
      if (requestedConnsRef.current.has(e.id)) continue;
      requestedConnsRef.current.add(e.id);
      getEntityConnections(e.id)
        .then(c => setConnMap(prev => ({ ...prev, [e.id]: briefsOf(c) })))
        .catch(() => setConnMap(prev => ({ ...prev, [e.id]: [] })));
    }
  }, [tiers.principals]);

  // ── Extraction ──
  const startExtraction = async (rebuild = false) => {
    if (rebuild && !window.confirm(
      'Rebuild the entity graph? All entities, relationships, events, and merge history for this matter will be deleted and re-extracted from scratch.',
    )) return;
    setExtractMsg(null);
    try {
      await triggerEntityExtraction(productionId, rebuild);
      setExtracting(true);
      setExtractMsg(rebuild
        ? 'Rebuild started — the old ontology is cleared; entities reappear below as documents are re-read.'
        : 'Extraction started — entities appear below as documents are processed.');
    } catch (e) {
      setExtractMsg(errText(e));
    }
  };

  // ── Docket machinery (capabilities unchanged from the old queue) ──

  const loadContext = (id: string) => {
    setCtxCache(prev => {
      if (prev[id]) return prev;
      getEntityMentions(id)
        .then(m => {
          const rows: CtxRow[] = [];
          for (const d of m.documents) {
            for (const mm of d.mentions) {
              if (rows.length >= 3) break;
              rows.push({ bates: d.bates_begin, text: mm.context_snippet || mm.surface_text, surface: mm.surface_text });
            }
            if (rows.length >= 3) break;
          }
          setCtxCache(p => ({ ...p, [id]: rows }));
        })
        .catch(() => setCtxCache(p => ({ ...p, [id]: [] })));
      return { ...prev, [id]: 'loading' };
    });
  };

  const keeperId = (s: MergeSuggestion): string => winners[s.id] ?? defaultKeeperId(s);
  const otherId = (s: MergeSuggestion): string => {
    const keeper = keeperId(s);
    return s.entity_a.id === keeper ? s.entity_b.id : s.entity_a.id;
  };

  const resolve = async (id: number, accept: boolean) => {
    setBusy(id);
    setResolveError(null);
    try {
      if (accept) await acceptMergeSuggestion(id);
      else await rejectMergeSuggestion(id);
      refreshAll();
    } catch (e) {
      setResolveError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  // Single-row "Same — merge": routes through the same mergeEntities(winner,
  // loser) path bulk merge uses, so the keeper radio choice is honored —
  // acceptMergeSuggestion instead picks the winner by mention count and
  // silently discards it.
  const mergeSuggestion = async (s: MergeSuggestion) => {
    setBusy(s.id);
    setResolveError(null);
    try {
      await mergeEntities(keeperId(s), otherId(s));
      refreshAll();
    } catch (e) {
      setResolveError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  const setKeeper = (id: number, entityId: string) => {
    setWinners(prev => ({ ...prev, [id]: entityId }));
  };

  const toggleRow = (id: number) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allSelected = suggestions.length > 0 && suggestions.every(s => selected.has(s.id));
  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(suggestions.map(s => s.id)));
  };

  const selectedRows = suggestions.filter(s => selected.has(s.id));
  const selectedCount = selectedRows.length;

  const mergeSelected = async () => {
    if (selectedRows.length === 0) return;
    setBulkBusy(true);
    setResolveError(null);
    try {
      // Sequential, not parallel: overlapping pairs (A~B and B~C sharing B)
      // can both be selected via select-all. Running them concurrently lets
      // separate transactions interleave and produce two EntityMerge rows
      // that snapshot the same loser, corrupting undo. One at a time keeps
      // each merge's re-point + snapshot atomic relative to the next.
      const failures: string[] = [];
      for (const s of selectedRows) {
        try {
          await mergeEntities(keeperId(s), otherId(s));
        } catch (e) {
          failures.push(errText(e));
        }
      }
      if (failures.length > 0) {
        setResolveError(`${failures.length} of ${selectedRows.length} merges failed: ${failures.join('; ')}`);
      }
      setSelected(new Set());
      refreshAll();
    } finally {
      setBulkBusy(false);
    }
  };

  const dismissSelected = async () => {
    if (selectedRows.length === 0) return;
    setBulkBusy(true);
    setResolveError(null);
    try {
      const results = await Promise.allSettled(selectedRows.map(s => rejectMergeSuggestion(s.id)));
      const failures = results.filter(r => r.status === 'rejected') as PromiseRejectedResult[];
      if (failures.length > 0) {
        setResolveError(`${failures.length} of ${selectedRows.length} dismissals failed: ${failures.map(f => errText(f.reason)).join('; ')}`);
      }
      setSelected(new Set());
      refreshAll();
    } finally {
      setBulkBusy(false);
    }
  };

  const runAutoTypos = async () => {
    setBulkBusy(true);
    setResolveError(null);
    setTypoMsg(null);
    try {
      const { merged } = await autoResolveTypos(productionId);
      setTypoMsg(`Merged ${merged} obvious typo${merged === 1 ? '' : 's'}`);
      setSelected(new Set());
      refreshAll();
    } catch (e) {
      setResolveError(errText(e));
    } finally {
      setBulkBusy(false);
    }
  };

  const openDocket = () => {
    setDocketOpen(true);
    docketRef.current?.scrollIntoView({ block: 'start' });
  };

  // ── Inline rename / delete. The same appliers back the EntityPanel's
  // rename/delete (via onRenamed/onDeleted), so panel curation reflects in
  // the cast immediately instead of leaving ghost rows and stale names. ──

  const applyRename = (id: string, name: string) => {
    setEntities(prev => prev.map(x => (x.id === id ? { ...x, canonical_name: name } : x)));
    setSuggestions(prev => prev.map(s => ({
      ...s,
      entity_a: s.entity_a.id === id ? { ...s.entity_a, canonical_name: name } : s.entity_a,
      entity_b: s.entity_b.id === id ? { ...s.entity_b, canonical_name: name } : s.entity_b,
    })));
  };

  const applyDelete = (id: string) => {
    setEntities(prev => prev.filter(x => x.id !== id));
    setTotal(t => Math.max(0, t - 1));
    setSuggestions(prev => prev.filter(s => s.entity_a.id !== id && s.entity_b.id !== id));
    if (openEntityId === id) openEntity(null);
  };

  const startRename = (e: EntityListItem) => {
    setEditingId(e.id);
    setConfirmingId(null);
    setDraftName(e.canonical_name);
    setRenameError(null);
  };

  const cancelRename = () => {
    setEditingId(null);
    setRenameError(null);
  };

  const saveRename = (e: EntityListItem) => {
    if (savingName) return;
    const trimmed = draftName.trim();
    if (!trimmed) { setRenameError('Name cannot be empty.'); return; }
    setSavingName(true);
    setRenameError(null);
    renameEntity(e.id, trimmed)
      .then(r => {
        applyRename(e.id, r.canonical_name);
        setSavingName(false);
        setEditingId(null);
        showToast('Entity renamed.', 'success');
      })
      .catch(err => {
        setSavingName(false);
        setRenameError(errText(err));
      });
  };

  const doDelete = (e: EntityListItem) => {
    if (deleteBusyId) return;
    setDeleteBusyId(e.id);
    deleteEntity(e.id)
      .then(() => {
        setDeleteBusyId(null);
        setConfirmingId(null);
        applyDelete(e.id);
        showToast('Entity deleted.', 'success');
      })
      .catch(err => {
        setDeleteBusyId(null);
        setConfirmingId(null);
        showToast(errText(err), 'error');
      });
  };

  // ── Render helpers ──

  const mergeName = (e: EntityListItem, isKeeper?: boolean) => (
    <span
      className="merge-name-wrap"
      onMouseEnter={() => { loadContext(e.id); setHoverCtxId(e.id); }}
      onMouseLeave={() => setHoverCtxId(prev => (prev === e.id ? null : prev))}
    >
      <button
        className="btn btn-ghost btn-xs"
        style={{ fontWeight: 600, textDecoration: isKeeper ? 'underline' : undefined }}
        onClick={() => openEntity(e.id)}
      >
        {entityDisplayName(e.canonical_name, e.entity_type)}
      </button>
      <span className="merge-count">{e.mention_count}×</span>
      {hoverCtxId === e.id && (
        <div className="merge-ctx-pop">
          {(!ctxCache[e.id] || ctxCache[e.id] === 'loading') && <span className="def-meta">Pulling context…</span>}
          {Array.isArray(ctxCache[e.id]) && (ctxCache[e.id] as CtxRow[]).length === 0 && (
            <span className="def-meta">No mention snippets on file.</span>
          )}
          {Array.isArray(ctxCache[e.id]) && (ctxCache[e.id] as CtxRow[]).map((r, i) => <CtxSnippet key={i} row={r} />)}
        </div>
      )}
    </span>
  );

  const renameEditor = (e: EntityListItem) => (
    <span className="cast-edit" onClick={ev => ev.stopPropagation()}>
      <input
        className="input input-sm"
        value={draftName}
        onChange={ev => setDraftName(ev.target.value)}
        onKeyDown={ev => {
          if (ev.key === 'Enter') { ev.preventDefault(); saveRename(e); }
          else if (ev.key === 'Escape') { ev.preventDefault(); cancelRename(); }
        }}
        disabled={savingName}
        autoFocus
        aria-label="Entity name"
      />
      <button className="btn btn-primary btn-xs" disabled={savingName} onClick={() => saveRename(e)}>
        {savingName ? 'Saving…' : 'Save'}
      </button>
      <button className="btn btn-ghost btn-xs" disabled={savingName} onClick={cancelRename}>Cancel</button>
      {renameError && <span className="cast-edit-error">{renameError}</span>}
    </span>
  );

  const rowActs = (e: EntityListItem) => (
    confirmingId === e.id ? (
      <span className="cast-confirm" onClick={ev => ev.stopPropagation()}>
        Delete this entity?
        <button className="cast-act is-danger" disabled={deleteBusyId === e.id} onClick={() => doDelete(e)}>
          {deleteBusyId === e.id ? 'Deleting…' : 'Delete'}
        </button>
        <button className="cast-act" onClick={() => setConfirmingId(null)}>Keep</button>
      </span>
    ) : (
      <span className="cast-acts" onClick={ev => ev.stopPropagation()}>
        <button className="cast-act" onClick={() => startRename(e)}>Rename</button>
        <button className="cast-act is-danger" onClick={() => setConfirmingId(e.id)}>Delete</button>
      </span>
    )
  );

  const dupChip = (id: string, dot = false) => (
    dupIds.has(id) ? (
      <button
        className={dot ? 'cast-dup-dot' : 'cast-dup'}
        title="The AI flagged this name as a possible duplicate — open the review docket"
        aria-label="Possible duplicate — open the review docket"
        onClick={ev => { ev.stopPropagation(); openDocket(); }}
      >
        {dot ? '' : 'Possible duplicate'}
      </button>
    ) : null
  );

  const rowKey = (id: string) => (ev: React.KeyboardEvent<HTMLDivElement>) => {
    if (ev.target !== ev.currentTarget) return;
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openEntity(id); }
  };

  const sectionStamp = (label: string, count: number) => (
    <div className="cast-stamp">
      <span className="cast-stamp-label">{label}</span>
      <span className="cast-stamp-rule" aria-hidden="true" />
      <span className="cast-stamp-tally">{count.toLocaleString()} {count === 1 ? 'name' : 'names'}</span>
    </div>
  );

  const renderPrincipal = (e: EntityListItem) => {
    const name = entityDisplayName(e.canonical_name, e.entity_type);
    const seg = scale && e.first_seen && e.last_seen ? railSegment(e, scale) : null;
    const conns = connMap[e.id];
    return (
      <article key={e.id} className={`cast-card is-${e.entity_type}`} onClick={() => openEntity(e.id)}>
        <header className="cast-card-top">
          <span className="cast-tile" aria-hidden="true">{initialsOf(name, e.entity_type)}</span>
          <div className="cast-card-id">
            {editingId === e.id ? renameEditor(e) : (
              <button className="cast-name" onClick={ev => { ev.stopPropagation(); openEntity(e.id); }}>
                {name}
              </button>
            )}
            <div className="cast-kind">
              {e.entity_type === 'person' ? 'Person' : 'Organization'}
              {e.role ? ` · ${e.role}` : ''}
            </div>
          </div>
          {editingId !== e.id && rowActs(e)}
        </header>
        <div className="cast-stats">
          {e.mention_count.toLocaleString()} MENTIONS · {e.document_count.toLocaleString()} DOCS
        </div>
        {dupChip(e.id)}
        {seg && e.first_seen && e.last_seen && (
          <div className="cast-rail-wrap" title="When this name appears in the chronology — first to last dated event">
            <div className="cast-rail"><span className="cast-rail-seg" style={seg} /></div>
            <div className="cast-rail-dates">{spanLabel(e.first_seen, e.last_seen)}</div>
          </div>
        )}
        {conns === undefined
          ? <span className="skel-redact cast-conn-skel" aria-hidden="true" />
          : conns.length > 0 && (
            <div className="cast-conns">
              {conns.map(c => (
                <button key={c.entityId} className="entity-chip"
                        onClick={ev => { ev.stopPropagation(); openEntity(c.entityId); }}>
                  <span className={`entity-dot entity-${c.entityType}`}>●</span>
                  {c.name}
                  <span className="cast-conn-rel">{c.rel}</span>
                </button>
              ))}
            </div>
          )}
      </article>
    );
  };

  const renderSupporting = (e: EntityListItem) => (
    <div key={e.id} className="cast-row" role="button" tabIndex={0}
         onClick={() => openEntity(e.id)} onKeyDown={rowKey(e.id)}>
      <span className={`entity-dot entity-${e.entity_type}`} aria-hidden="true">●</span>
      {editingId === e.id ? renameEditor(e) : (
        <div className="cast-row-main">
          <span className="cast-row-name">{entityDisplayName(e.canonical_name, e.entity_type)}</span>
          {e.role && <span className="cast-row-role">{e.role}</span>}
        </div>
      )}
      {dupChip(e.id, true)}
      <span className="cast-row-stats">{e.mention_count.toLocaleString()}× · {e.document_count.toLocaleString()} docs</span>
      {e.first_seen && e.last_seen && (
        <span className="cast-row-span" title="First to last dated event">{spanLabel(e.first_seen, e.last_seen)}</span>
      )}
      {editingId !== e.id && rowActs(e)}
    </div>
  );

  const renderMentioned = (e: EntityListItem) => (
    <div key={e.id} className="cast-line" role="button" tabIndex={0}
         onClick={() => openEntity(e.id)} onKeyDown={rowKey(e.id)}>
      <span className={`entity-dot entity-${e.entity_type}`} aria-hidden="true">●</span>
      {editingId === e.id ? renameEditor(e) : (
        <span className="cast-line-name">{entityDisplayName(e.canonical_name, e.entity_type)}</span>
      )}
      {dupChip(e.id, true)}
      <span className="cast-line-count">{e.mention_count}×</span>
      {editingId !== e.id && rowActs(e)}
    </div>
  );

  // ── Page ──

  const countLabel = filtered
    ? `${total.toLocaleString()} match${total === 1 ? '' : 'es'}`
    : `${total.toLocaleString()} named in the record`;
  const showEmpty = !loading && !loadError && total === 0;
  const allShown = !loading && !loadError && !hasMore && entities.length > 0;

  return (
    <div className="cast">
      <div className="cast-bar">
        <div className="cast-bar-row">
          <button className="btn btn-ghost btn-xs" onClick={onBack}>← Back</button>
          <div className="cast-heading">
            <span className="cast-title">People &amp; Organizations</span>
            <span className="cast-count">{countLabel}</span>
          </div>
          <span className="bates-chip">DRAMATIS&nbsp;PERSONAE</span>
          <div className="cast-spacer" />
          <button
            className="btn btn-xs"
            disabled={extracting}
            onClick={() => startExtraction(false)}
            title="Run AI entity extraction over this matter's documents (manager only)"
          >
            {extracting ? 'Extracting…' : 'Extract entities'}
          </button>
          <button
            className="btn btn-ghost btn-xs"
            disabled={extracting}
            onClick={() => startExtraction(true)}
            title="Delete this matter's entire ontology and re-extract from scratch (manager only)"
          >
            Rebuild
          </button>
        </div>

        <div className="cast-bar-row">
          <div className="cast-search">
            <span className="cast-search-mark" aria-hidden="true">⌕</span>
            <input
              className="input input-sm cast-search-input"
              type="text"
              placeholder="Find a name"
              aria-label="Search people and organizations"
              value={searchInput}
              onChange={ev => setSearchInput(ev.target.value)}
            />
            {searchInput && (
              <button className="cast-search-clear" aria-label="Clear the search"
                      onClick={() => { setSearchInput(''); if (query) { setQuery(''); resetPaging(true); } }}>
                ×
              </button>
            )}
          </div>

          <div className="cast-seg" role="group" aria-label="Filter by kind of actor">
            {TYPE_LENSES.map(l => (
              <button key={l.value}
                      className={`cast-seg-btn${typeFilter === l.value ? ' is-on' : ''}`}
                      aria-pressed={typeFilter === l.value}
                      onClick={() => {
                        if (typeFilter === l.value) return;
                        setTypeFilter(l.value);
                        resetPaging(true);
                      }}>
                {l.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {extractMsg && <div className="cast-extract-note">{extractMsg}</div>}

      <div className="cast-scroll" ref={scrollRef}>
        <div className="cast-track">
          {suggestions.length > 0 && (
            <section className="docket" ref={docketRef} aria-label="Merge review docket">
              <div className="docket-bar">
                <button className="docket-toggle" aria-expanded={docketOpen}
                        onClick={() => setDocketOpen(v => !v)}>
                  <span className="docket-title">Review docket</span>
                  <span className="docket-count">
                    {suggestions.length} possible duplicate{suggestions.length === 1 ? '' : 's'}
                  </span>
                  <span className="docket-chevron" aria-hidden="true">{docketOpen ? '▾' : '▸'}</span>
                </button>
                <span className="docket-note">The AI thinks these may be the same. Nothing merges until you say so.</span>
                <button
                  className="btn btn-ghost btn-xs"
                  disabled={bulkBusy}
                  onClick={runAutoTypos}
                  title="Auto-merge pairs that differ by a single-character typo (safe class only)"
                >
                  Auto-merge obvious typos
                </button>
              </div>
              {typoMsg && <div className="docket-msg">{typoMsg}</div>}
              {resolveError && <div className="docket-msg is-error">{resolveError}</div>}
              {docketOpen && (
                <div className="docket-body">
                  <div className="docket-bulk">
                    <label className="docket-bulk-all" title="Select all suggestions">
                      <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                      Select all
                    </label>
                    <button className="btn btn-xs" disabled={bulkBusy || selectedCount === 0} onClick={mergeSelected}>
                      Merge selected ({selectedCount})
                    </button>
                    <button className="btn btn-ghost btn-xs" disabled={bulkBusy || selectedCount === 0} onClick={dismissSelected}>
                      Dismiss selected ({selectedCount})
                    </button>
                  </div>
                  {suggestions.map(s => {
                    const keeper = keeperId(s);
                    const rowBusy = busy === s.id || bulkBusy;
                    return (
                      <div key={s.id} className="docket-row">
                        <div className="docket-pair">
                          <input
                            type="checkbox"
                            checked={selected.has(s.id)}
                            onChange={() => toggleRow(s.id)}
                            title="Select for bulk action"
                          />
                          <label style={{ display: 'inline-flex', alignItems: 'center', cursor: 'pointer' }} title="Keep this spelling as canonical">
                            <input
                              type="radio"
                              name={`keeper-${s.id}`}
                              checked={keeper === s.entity_a.id}
                              onChange={() => setKeeper(s.id, s.entity_a.id)}
                            />
                          </label>
                          {mergeName(s.entity_a, keeper === s.entity_a.id)}
                          <span className="docket-vs">↔</span>
                          <label style={{ display: 'inline-flex', alignItems: 'center', cursor: 'pointer' }} title="Keep this spelling as canonical">
                            <input
                              type="radio"
                              name={`keeper-${s.id}`}
                              checked={keeper === s.entity_b.id}
                              onChange={() => setKeeper(s.id, s.entity_b.id)}
                            />
                          </label>
                          {mergeName(s.entity_b, keeper === s.entity_b.id)}
                          <span className="docket-row-acts">
                            <button className="btn btn-secondary btn-xs" disabled={rowBusy} onClick={() => mergeSuggestion(s)}>Same — merge</button>
                            <button className="btn btn-ghost btn-xs" disabled={rowBusy} onClick={() => resolve(s.id, false)}>Different</button>
                          </span>
                        </div>
                        <div className="docket-rationale">"{s.rationale}"</div>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          )}

          {tiers.principals.length > 0 && (
            <section aria-label="Principals">
              {sectionStamp('Principals', tiers.principals.length)}
              <div className="cast-principals">{tiers.principals.map(renderPrincipal)}</div>
            </section>
          )}

          {tiers.supporting.length > 0 && (
            <section aria-label="Supporting cast">
              {sectionStamp('Supporting cast', tiers.supporting.length)}
              <div className="cast-supporting">{tiers.supporting.map(renderSupporting)}</div>
            </section>
          )}

          {tiers.mentioned.length > 0 && (
            <section aria-label="Also named in the record">
              {sectionStamp('Also named in the record', tiers.mentioned.length)}
              <div className="cast-index">{tiers.mentioned.map(renderMentioned)}</div>
            </section>
          )}

          <div ref={sentinelRef} className="cast-sentinel" aria-hidden="true" />

          {loading && entities.length === 0 && (
            <div className="cast-skel" aria-hidden="true">
              <span className="skel-redact" style={{ width: '45%', height: '1.4em' }} />
              <span className="skel-redact" style={{ width: '70%' }} />
              <span className="skel-redact" style={{ width: '60%' }} />
              <span className="skel-redact" style={{ width: '30%' }} />
            </div>
          )}
          {loading && entities.length > 0 && <div className="cast-status">Loading…</div>}

          {loadError && (
            <div className="cast-status is-error">
              Couldn&rsquo;t load the cast. {loadError}
              <button className="btn btn-secondary btn-xs" onClick={refreshAll}>Try again</button>
            </div>
          )}

          {showEmpty && !filtered && (
            <div className="empty-state">
              <div style={{ fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)', fontWeight: 700 }}>
                No cast of characters yet.
              </div>
              <div style={{ maxWidth: '46ch' }}>
                The AI reads the corpus and builds it: every person and organization, resolved
                across aliases, every relationship cited to its document.
              </div>
              <button className="btn btn-primary btn-sm" style={{ marginTop: 8 }} disabled={extracting}
                      onClick={() => startExtraction(false)}>
                {extracting ? 'Extracting…' : 'Extract entities'}
              </button>
            </div>
          )}

          {showEmpty && filtered && (
            <div className="empty-state">
              No names match. Clear the search or switch back to Everyone to see the full cast.
            </div>
          )}

          {allShown && <div className="cast-end">End of the cast list.</div>}
        </div>
      </div>

      {openEntityId && (
        <EntityPanel
          entityId={openEntityId}
          onClose={() => openEntity(null)}
          onOpenEntity={openEntity}
          onOpenDocument={docId => { openEntity(null); onViewDocument(docId); }}
          onRenamed={(id, name) => applyRename(id, name)}
          onDeleted={applyDelete}
        />
      )}
    </div>
  );
}
