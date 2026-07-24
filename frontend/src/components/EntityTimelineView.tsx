import { useEffect, useMemo, useRef, useState } from 'react';
import { deleteEvent, getTimeline, listEntities, updateEvent } from '../api/client';
import type { DatePrecision, EntityListItem, TimelineEvent } from '../types';
import EntityPanel from './EntityPanel';
import { showToast } from './Toast';

interface Props {
  productionId: number;
  openEntityId?: string | null;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
  onOpenEntityChange?: (id: string | null) => void;
}

const PER_PAGE = 40;
/** The API's own default — events rated 3+ are the ones worth reading first. */
const KEY_EVENTS_MIN = 3;
/** The entities endpoint caps per_page at 100; hold a local window this deep. */
const ENTITY_PAGE = 100;
const ENTITY_INDEX_PAGES = 3;
const MAX_OPTIONS = 60;

const TYPE_LABELS: Record<string, string> = {
  meeting: 'Meeting', communication: 'Communication', payment: 'Payment',
  filing: 'Filing', agreement: 'Agreement', other: 'Event',
};

type Tier = 'pivotal' | 'key' | 'notable' | 'routine';

/** Significance 1-5 → the visual tier that drives glyph, card and type weight. */
function tierOf(significance: number): Tier {
  if (significance >= 5) return 'pivotal';
  if (significance >= 4) return 'key';
  if (significance >= 3) return 'notable';
  return 'routine';
}

const TIER_LEGEND: { tier: Tier; label: string }[] = [
  { tier: 'pivotal', label: 'Pivotal' },
  { tier: 'key', label: 'Key' },
  { tier: 'notable', label: 'Notable' },
  { tier: 'routine', label: 'Routine' },
];

function parseIso(iso: string): Date {
  return new Date(iso + 'T00:00:00');
}

/** Gutter date. Month/year precision is prefixed "c." — circa, as a litigator writes it. */
function dateLabel(e: TimelineEvent): { text: string; circa: boolean } {
  if (!e.event_date) return { text: '—', circa: false };
  const d = parseIso(e.event_date);
  if (e.date_precision === 'year') return { text: String(d.getFullYear()), circa: true };
  if (e.date_precision === 'month') {
    return { text: d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' }), circa: true };
  }
  return { text: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), circa: false };
}

/**
 * Chapter stamp: month over year for dated events, the year alone when only the
 * year is established. Stacked rather than inline so a long month name never
 * wraps the narrow gutter.
 */
function chapterOf(e: TimelineEvent): { key: string; month: string; year: string } {
  if (!e.event_date) return { key: 'Undated', month: 'Undated', year: '' };
  const d = parseIso(e.event_date);
  const year = String(d.getFullYear());
  if (e.date_precision === 'year') return { key: year, month: year, year: '' };
  const month = d.toLocaleDateString(undefined, { month: 'long' });
  return { key: `${month} ${year}`, month, year };
}

/** Server order: date ascending, undated last, id as the tiebreak. */
function sortEvents(list: TimelineEvent[]): TimelineEvent[] {
  return [...list].sort((a, b) => {
    if (a.event_date && b.event_date) {
      if (a.event_date !== b.event_date) return a.event_date < b.event_date ? -1 : 1;
    } else if (a.event_date !== b.event_date) {
      return a.event_date ? -1 : 1;
    }
    return a.event_id - b.event_id;
  });
}

function mergePage(prev: TimelineEvent[], incoming: TimelineEvent[]): TimelineEvent[] {
  const seen = new Set(prev.map(e => e.event_id));
  return [...prev, ...incoming.filter(e => !seen.has(e.event_id))];
}

/** Trim an ISO date to the shape the API stores for the chosen precision. */
function toPrecision(iso: string, precision: DatePrecision): string {
  if (precision === 'year') return iso.slice(0, 4);
  if (precision === 'month') return iso.slice(0, 7);
  return iso;
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export default function EntityTimelineView({ productionId, openEntityId, onViewDocument, onBack, onOpenEntityChange }: Props) {
  // ── Chronology data ──
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [undatedCount, setUndatedCount] = useState(0);
  const [page, setPage] = useState(1);
  // Highest page whose request has settled. `loading` is derived from it, so
  // nothing has to flip a loading flag inside an effect body.
  const [settledPage, setSettledPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);

  // ── Lenses ──
  const [showAll, setShowAll] = useState(false);
  const [typeFilter, setTypeFilter] = useState('');
  const [selectedEntity, setSelectedEntity] = useState<EntityListItem | null>(null);
  const [showUndated, setShowUndated] = useState(false);

  // ── Entity type-ahead ──
  const [entityIndex, setEntityIndex] = useState<EntityListItem[]>([]);
  const [entityTotal, setEntityTotal] = useState(0);
  const [remoteMatches, setRemoteMatches] = useState<EntityListItem[]>([]);
  const [entityQuery, setEntityQuery] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeOption, setActiveOption] = useState(0);

  // ── Per-event editing ──
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draftDate, setDraftDate] = useState('');
  const [draftPrecision, setDraftPrecision] = useState<DatePrecision>('day');
  const [editError, setEditError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmingId, setConfirmingId] = useState<number | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);

  const entityFilter = selectedEntity?.id ?? '';
  const minSignificance = showAll ? 1 : KEY_EVENTS_MIN;
  const loading = settledPage < page;
  const indexComplete = entityIndex.length >= entityTotal;

  const openEntity = (id: string | null) => { onOpenEntityChange?.(id); };

  // ── Load the entity index for the type-ahead ──
  useEffect(() => {
    let cancelled = false;
    listEntities(productionId, undefined, undefined, 1, ENTITY_PAGE)
      .then(async r => {
        if (cancelled) return;
        setEntityIndex(r.entities);
        setEntityTotal(r.total);
        // Entities come back ordered by mention count, so the first pages hold
        // the names anyone is likely to filter by. Pull a few more so typing
        // resolves locally; anything past that falls back to a server search.
        const pages = Math.min(ENTITY_INDEX_PAGES, Math.ceil(r.total / ENTITY_PAGE));
        for (let p = 2; p <= pages; p++) {
          const more = await listEntities(productionId, undefined, undefined, p, ENTITY_PAGE);
          if (cancelled) return;
          setEntityIndex(prev => mergeEntities(prev, more.entities));
        }
      })
      .catch(e => console.warn('listEntities failed:', e));
    return () => { cancelled = true; };
  }, [productionId]);

  // Backfill matches that live past the local window, debounced.
  useEffect(() => {
    const q = entityQuery.trim();
    if (indexComplete || q.length < 2) return;
    const timer = setTimeout(() => {
      listEntities(productionId, q, undefined, 1, ENTITY_PAGE)
        .then(r => setRemoteMatches(r.entities))
        .catch(() => { /* local matches still stand */ });
    }, 220);
    return () => clearTimeout(timer);
  }, [productionId, entityQuery, indexComplete]);

  // ── Load a page of the chronology ──
  useEffect(() => {
    let cancelled = false;
    getTimeline(productionId, entityFilter || undefined, typeFilter || undefined,
                page, PER_PAGE, minSignificance)
      .then(r => {
        if (cancelled) return;
        setEvents(prev => (page === 1 ? r.events : mergePage(prev, r.events)));
        setTotal(r.total);
        setUndatedCount(r.undated_count);
        // A short page means the server has nothing left, whatever the count says.
        setHasMore(r.events.length === PER_PAGE && (page - 1) * PER_PAGE + r.events.length < r.total);
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
  }, [productionId, entityFilter, typeFilter, minSignificance, page, retryTick]);

  // ── Infinite scroll ──
  // The observer only exists while there is genuinely a next page to fetch and
  // nothing is in flight, so a page can't be requested twice. Re-arming after a
  // load fires immediately if the sentinel is still on screen, which is how a
  // short viewport keeps filling.
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

  const options = useMemo(() => {
    const q = entityQuery.trim().toLowerCase();
    const local = q
      ? entityIndex.filter(e => e.canonical_name.toLowerCase().includes(q))
      : entityIndex;
    if (!q) return local.slice(0, MAX_OPTIONS);
    const seen = new Set(local.map(e => e.id));
    const remote = remoteMatches.filter(
      e => !seen.has(e.id) && e.canonical_name.toLowerCase().includes(q));
    return [...local, ...remote].slice(0, MAX_OPTIONS);
  }, [entityIndex, remoteMatches, entityQuery]);

  const dated = useMemo(() => events.filter(e => e.event_date), [events]);
  const undated = useMemo(() => events.filter(e => !e.event_date), [events]);

  const chapters = useMemo(() => {
    const out: { key: string; month: string; year: string; items: TimelineEvent[] }[] = [];
    for (const e of dated) {
      const stamp = chapterOf(e);
      const last = out[out.length - 1];
      if (last && last.key === stamp.key) last.items.push(e);
      else out.push({ ...stamp, items: [e] });
    }
    return out;
  }, [dated]);

  // ── Handlers (all state resets live here, never in an effect) ──

  const resetPaging = () => {
    setEvents([]);
    setPage(1);
    setSettledPage(0);
    setHasMore(false);
    setLoadError(null);
    setEditingId(null);
    setConfirmingId(null);
    scrollRef.current?.scrollTo({ top: 0 });
  };

  const changeLens = (all: boolean) => {
    if (all === showAll) return;
    setShowAll(all);
    resetPaging();
  };

  const changeType = (value: string) => {
    if (value === typeFilter) return;
    setTypeFilter(value);
    resetPaging();
  };

  const chooseEntity = (entity: EntityListItem | null) => {
    setSelectedEntity(entity);
    setEntityQuery(entity ? entity.canonical_name : '');
    setMenuOpen(false);
    setActiveOption(0);
    // Re-picking the entity already in force changes no fetch input, so the
    // load effect would not re-run and the reset would strand `loading`.
    if ((entity?.id ?? '') === entityFilter) return;
    resetPaging();
  };

  const onSearchKeyDown = (ev: React.KeyboardEvent<HTMLInputElement>) => {
    if (ev.key === 'Escape') { setMenuOpen(false); return; }
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      setMenuOpen(true);
      setActiveOption(i => Math.min(i + 1, options.length - 1));
      return;
    }
    if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      setActiveOption(i => Math.max(i - 1, 0));
      return;
    }
    if (ev.key === 'Enter' && menuOpen && options[activeOption]) {
      ev.preventDefault();
      chooseEntity(options[activeOption]);
    }
  };

  const retry = () => {
    setLoadError(null);
    setSettledPage(0);
    setRetryTick(t => t + 1);
  };

  const startEdit = (e: TimelineEvent) => {
    setEditingId(e.event_id);
    setConfirmingId(null);
    setDraftDate(e.event_date ?? '');
    setDraftPrecision(e.date_precision === 'unknown' ? 'day' : e.date_precision);
    setEditError(null);
  };

  const applyDate = (id: number, wasUndated: boolean, nextDate: string | null, nextPrecision: DatePrecision) => {
    setEvents(prev => sortEvents(prev.map(
      e => (e.event_id === id ? { ...e, event_date: nextDate, date_precision: nextPrecision } : e))));
    const nowUndated = nextDate === null;
    if (wasUndated !== nowUndated) {
      setUndatedCount(c => (nowUndated ? c + 1 : Math.max(0, c - 1)));
    }
  };

  const saveDate = (e: TimelineEvent) => {
    if (!draftDate) { setEditError('Pick a date, or clear it instead.'); return; }
    setSaving(true);
    setEditError(null);
    updateEvent(e.event_id, {
      event_date: toPrecision(draftDate, draftPrecision),
      date_precision: draftPrecision,
    })
      .then(r => {
        applyDate(e.event_id, !e.event_date, r.event_date, r.date_precision);
        setSaving(false);
        setEditingId(null);
        showToast('Date corrected.', 'success');
      })
      .catch(err => { setSaving(false); setEditError(errText(err)); });
  };

  const clearDate = (e: TimelineEvent) => {
    setSaving(true);
    setEditError(null);
    updateEvent(e.event_id, { event_date: null })
      .then(r => {
        applyDate(e.event_id, !e.event_date, r.event_date, r.date_precision);
        setSaving(false);
        setEditingId(null);
        showToast('Date cleared — the event moved to Undated.', 'success');
      })
      .catch(err => { setSaving(false); setEditError(errText(err)); });
  };

  const removeEvent = (e: TimelineEvent) => {
    const wasUndated = !e.event_date;
    deleteEvent(e.event_id)
      .then(() => {
        setEvents(prev => prev.filter(x => x.event_id !== e.event_id));
        setTotal(t => Math.max(0, t - 1));
        if (wasUndated) setUndatedCount(c => Math.max(0, c - 1));
        setConfirmingId(null);
        showToast('Event deleted.', 'success');
      })
      .catch(err => { setConfirmingId(null); showToast(errText(err), 'error'); });
  };

  // ── Rendering ──

  const noun = showAll ? 'events' : 'key events';
  const countLabel = total === 0
    ? `No ${noun}`
    : events.length >= total ? `${total} ${noun}` : `${events.length} of ${total} ${noun}`;
  const filtered = Boolean(entityFilter || typeFilter);

  const renderRow = (e: TimelineEvent) => {
    const tier = tierOf(e.significance);
    const { text, circa } = dateLabel(e);
    const editing = editingId === e.event_id;
    const confirming = confirmingId === e.event_id;
    return (
      <div key={e.event_id} className={`chrono-row is-${tier}`}>
        <div className="chrono-date">
          {circa && <span className="chrono-circa" title="Circa — only the month or year is established">c. </span>}
          {text}
        </div>
        <div className="chrono-node">
          <span className={`chrono-glyph is-${tier}`} aria-hidden="true" />
        </div>
        <div className="chrono-body">
          <div className={`chrono-card is-${tier}`}>
            <div className="chrono-card-head">
              <span className="chrono-type">{TYPE_LABELS[e.event_type] || e.event_type}</span>
              <div className="chrono-acts">
                {confirming ? (
                  <span className="chrono-confirm">
                    Delete this event?
                    <button className="chrono-act is-danger" onClick={() => removeEvent(e)}>Delete</button>
                    <button className="chrono-act" onClick={() => setConfirmingId(null)}>Keep</button>
                  </span>
                ) : (
                  <>
                    <button className="chrono-act" onClick={() => startEdit(e)}>Correct date</button>
                    <button className="chrono-act is-danger" onClick={() => setConfirmingId(e.event_id)}>Delete</button>
                  </>
                )}
              </div>
            </div>

            <div className="chrono-desc">{e.description}</div>

            {e.date_source_text && (
              <q className="chrono-source" title="The phrase in the document this date was read from">
                {e.date_source_text}
              </q>
            )}
            {!e.date_source_text && e.event_date && (
              <div className="chrono-unsourced" title="No phrase in the document backs this date. Check it before relying on it.">
                Date not traced to the document
              </div>
            )}

            {editing && (
              <div className="chrono-edit">
                <span className="chrono-edit-label">Date</span>
                <input className="input input-sm" type="date" value={draftDate}
                       onChange={ev => setDraftDate(ev.target.value)} style={{ maxWidth: 160 }} />
                <select className="input input-sm" value={draftPrecision} style={{ maxWidth: 130 }}
                        onChange={ev => setDraftPrecision(ev.target.value as DatePrecision)}>
                  <option value="day">Exact day</option>
                  <option value="month">Month only</option>
                  <option value="year">Year only</option>
                </select>
                <button className="btn btn-primary btn-xs" disabled={saving} onClick={() => saveDate(e)}>
                  {saving ? 'Saving…' : 'Save'}
                </button>
                <button className="btn btn-ghost btn-xs" disabled={saving} onClick={() => clearDate(e)}>Clear date</button>
                <button className="btn btn-ghost btn-xs" disabled={saving} onClick={() => setEditingId(null)}>Cancel</button>
                {draftDate && (
                  <div className="chrono-edit-hint">Records as {toPrecision(draftDate, draftPrecision)}</div>
                )}
                {editError && <div className="chrono-edit-error">{editError}</div>}
              </div>
            )}

            <div className="chrono-meta">
              <button className="chrono-cite" onClick={() => onViewDocument(e.document_id)}
                      title="Open the source document">
                {e.bates_begin}{e.title ? ` · ${e.title}` : ''}
              </button>
              {e.participants.map(p => (
                <button key={p.entity_id} className="chrono-party" onClick={() => openEntity(p.entity_id)}>
                  <span className={`entity-dot entity-${p.entity_type}`}>●</span>
                  {p.canonical_name}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="chrono">
      <div className="chrono-bar">
        <div className="chrono-bar-row">
          <button className="btn btn-ghost btn-xs" onClick={onBack}>← Back</button>
          <div className="chrono-heading">
            <span className="chrono-title">Chronology</span>
            <span className="chrono-count">{countLabel}</span>
          </div>
          <div className="chrono-spacer" />
          <div className="chrono-seg" role="group" aria-label="How much of the record to show">
            <button className={`chrono-seg-btn${showAll ? '' : ' is-on'}`}
                    aria-pressed={!showAll} onClick={() => changeLens(false)}>
              Key events
            </button>
            <button className={`chrono-seg-btn${showAll ? ' is-on' : ''}`}
                    aria-pressed={showAll} onClick={() => changeLens(true)}>
              All events
            </button>
          </div>
        </div>

        <div className="chrono-bar-row">
          <div className="chrono-search">
            <span className="chrono-search-mark" aria-hidden="true">⌕</span>
            <input
              className="input input-sm chrono-search-input"
              type="text"
              role="combobox"
              aria-expanded={menuOpen}
              aria-autocomplete="list"
              aria-label="Filter by person or organization"
              placeholder="Filter by person or organization"
              value={entityQuery}
              onChange={ev => { setEntityQuery(ev.target.value); setMenuOpen(true); setActiveOption(0); }}
              onFocus={() => setMenuOpen(true)}
              onBlur={() => setMenuOpen(false)}
              onKeyDown={onSearchKeyDown}
            />
            {(entityQuery || selectedEntity) && (
              <button className="chrono-search-clear" aria-label="Clear the person filter"
                      onClick={() => chooseEntity(null)}>×</button>
            )}
            {menuOpen && (
              // Keep focus on the input so the blur handler doesn't close the
              // menu before a click on an option registers.
              <div className="chrono-menu" role="listbox" onMouseDown={ev => ev.preventDefault()}>
                {selectedEntity && (
                  <button className="chrono-opt" role="option" aria-selected={false}
                          onClick={() => chooseEntity(null)}>
                    <span className="chrono-opt-name">Everyone in this production</span>
                  </button>
                )}
                {options.map((o, i) => (
                  <button key={o.id} role="option" aria-selected={o.id === entityFilter}
                          className={`chrono-opt${i === activeOption ? ' is-active' : ''}`}
                          onClick={() => chooseEntity(o)}>
                    <span className={`entity-dot entity-${o.entity_type}`}>●</span>
                    <span className="chrono-opt-name">{o.canonical_name}</span>
                    <span className="chrono-opt-count">{o.mention_count}</span>
                  </button>
                ))}
                {options.length === 0 && (
                  <div className="chrono-menu-note">
                    {entityIndex.length === 0 ? 'No entities extracted yet.' : 'No names match that.'}
                  </div>
                )}
              </div>
            )}
          </div>

          <select className="input input-sm" value={typeFilter} style={{ maxWidth: 160 }}
                  aria-label="Filter by event type"
                  onChange={ev => changeType(ev.target.value)}>
            <option value="">Every kind of event</option>
            {Object.entries(TYPE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>

          <div className="chrono-legend" title="Significance, rated when the event was extracted">
            {TIER_LEGEND.map(l => (
              <span key={l.tier} className="chrono-legend-item">
                <span className={`chrono-glyph is-${l.tier}`} aria-hidden="true" />
                {l.label}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="chrono-scroll" ref={scrollRef}>
        <div className="chrono-track">
          {chapters.length > 0 && (
            <div className="chrono-spine">
              {chapters.map(c => (
                <div key={c.key}>
                  <div className="chrono-chapter">
                    <div className="chrono-chapter-label">
                      <span className="chrono-chapter-month">{c.month}</span>
                      {c.year && <span className="chrono-chapter-year">{c.year}</span>}
                    </div>
                    <div className="chrono-chapter-notch" aria-hidden="true" />
                    <div className="chrono-chapter-rule">
                      <span className="chrono-chapter-tally">
                        {c.items.length} {c.items.length === 1 ? 'entry' : 'entries'}
                      </span>
                    </div>
                  </div>
                  {c.items.map(renderRow)}
                </div>
              ))}
            </div>
          )}

          <div ref={sentinelRef} className="chrono-sentinel" aria-hidden="true" />

          {loading && <div className="chrono-status">Loading…</div>}

          {loadError && (
            <div className="chrono-status is-error">
              Couldn&rsquo;t load the chronology. {loadError}
              <button className="btn btn-secondary btn-xs" onClick={retry}>Try again</button>
            </div>
          )}

          {!loading && !loadError && total === 0 && (
            <div className="empty-state">
              {filtered
                ? 'No events match these filters.'
                : showAll
                  ? 'No chronology yet. Run entity extraction from the Entities view.'
                  : 'No key events found. Switch to All events to see routine entries.'}
            </div>
          )}

          {undatedCount > 0 && (
            <div className="chrono-undated">
              <button className="chrono-undated-toggle" aria-expanded={showUndated}
                      onClick={() => setShowUndated(v => !v)}>
                <span className="chrono-undated-title">
                  {showUndated ? '▾' : '▸'} Undated
                </span>
                <span className="chrono-count">{undatedCount} {undatedCount === 1 ? 'event' : 'events'}</span>
              </button>
              {showUndated && (
                <>
                  <p className="chrono-undated-note">
                    No year could be established from the document, so these sit off the timeline.
                    Correct a date to place one.
                  </p>
                  {undated.length > 0
                    ? undated.map(renderRow)
                    : <p className="chrono-undated-note">Keep scrolling the chronology to load them.</p>}
                </>
              )}
            </div>
          )}

          {!loading && !loadError && !hasMore && dated.length > 0 && (
            <div className="chrono-end">End of the record.</div>
          )}
        </div>
      </div>

      {openEntityId && (
        <EntityPanel entityId={openEntityId} onClose={() => openEntity(null)}
                     onOpenEntity={openEntity}
                     onOpenDocument={docId => { openEntity(null); onViewDocument(docId); }} />
      )}
    </div>
  );
}

function mergeEntities(prev: EntityListItem[], incoming: EntityListItem[]): EntityListItem[] {
  const seen = new Set(prev.map(e => e.id));
  return [...prev, ...incoming.filter(e => !seen.has(e.id))];
}
