import { useCallback, useEffect, useRef, useState } from 'react';
import { getByBates, getClusterDocuments, getPipeline, runPipeline } from '../api/client';
import { showToast } from './Toast';
import type { ClusterDocument, ClusterInfo, PipelineInfo, PipelineStageState, PipelineStatus, ProductionInfo } from '../types';

const POLL_MS = 5000;

// A worker killed mid-stage (e.g. Cloud Run scale-down) leaves a stage
// "running" forever with no further status writes. Mirrors the backend's
// STALE_RUNNING_MINUTES (backend/app/routers/productions.py) — the Cloud
// Tasks dispatch ceiling is 30 minutes, so 45 gives margin.
const STALE_RUNNING_MINUTES = 45;

function briefCollapseKey(productionId: number) {
  return `vigilist.brief.collapsed.${productionId}`;
}

function safeGet(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function safeSet(key: string, value: string) {
  try { window.localStorage.setItem(key, value); } catch { /* storage unavailable */ }
}

function anyStageRunning(status: PipelineStatus | null | undefined): boolean {
  if (!status) return false;
  return status.clustering === 'running' || status.summaries === 'running' || status.brief === 'running';
}

/**
 * Same as `anyStageRunning`, but a "running" status whose `updated_at` is
 * older than the stale threshold is treated as not-running — the worker
 * that would have advanced it is presumed dead. Drives `isRunning` and
 * state selection; freshly-started runs (recent `updated_at`, or none yet
 * written) still count as running.
 */
function isActivelyRunning(status: PipelineStatus | null | undefined): boolean {
  if (!anyStageRunning(status)) return false;
  const ts = status?.updated_at;
  if (!ts) return true;
  const updated = new Date(ts).getTime();
  if (Number.isNaN(updated)) return true;
  return Date.now() - updated < STALE_RUNNING_MINUTES * 60 * 1000;
}

function anyStageFailed(status: PipelineStatus | null | undefined): boolean {
  if (!status) return false;
  return status.clustering === 'failed' || status.summaries === 'failed' || status.brief === 'failed';
}

function stageGlyph(state: PipelineStageState | undefined): string {
  switch (state) {
    case 'done': return '✓';
    case 'running': return '…';
    case 'failed': return '!';
    default: return '·';
  }
}

interface DonutSlice {
  id: number;
  path: string;
  colorIndex: number;
}

/**
 * Same arc-path trigonometry as the deleted CorpusAnalysis.tsx DonutChart,
 * recolored to the theme tokens (no hex literals here — colors come from
 * `var(--theme-N)`). Pulled out of the component body (a plain helper, not a
 * component the React Compiler analyzes) because it accumulates `startAngle`
 * across iterations — a mutation the compiler forbids inside component render.
 */
function buildDonutSlices(clusters: ClusterInfo[], total: number, cx: number, cy: number, outerR: number, innerR: number): DonutSlice[] {
  const slices: DonutSlice[] = [];
  let startAngle = -Math.PI / 2;

  clusters.forEach((c, i) => {
    const angle = (c.doc_count / total) * 2 * Math.PI;
    const endAngle = startAngle + angle;
    const largeArc = angle > Math.PI ? 1 : 0;

    const x1 = cx + outerR * Math.cos(startAngle);
    const y1 = cy + outerR * Math.sin(startAngle);
    const x2 = cx + outerR * Math.cos(endAngle);
    const y2 = cy + outerR * Math.sin(endAngle);
    const ix1 = cx + innerR * Math.cos(endAngle);
    const iy1 = cy + innerR * Math.sin(endAngle);
    const ix2 = cx + innerR * Math.cos(startAngle);
    const iy2 = cy + innerR * Math.sin(startAngle);

    const path = `M ${x1} ${y1} A ${outerR} ${outerR} 0 ${largeArc} 1 ${x2} ${y2} L ${ix1} ${iy1} A ${innerR} ${innerR} 0 ${largeArc} 0 ${ix2} ${iy2} Z`;

    slices.push({ id: c.id, path, colorIndex: i });
    startAngle = endAngle;
  });

  return slices;
}

function ThemeDonut({ clusters, size = 160 }: { clusters: ClusterInfo[]; size?: number }) {
  const total = clusters.reduce((s, c) => s + c.doc_count, 0);
  if (total === 0) return null;

  const cx = size / 2;
  const cy = size / 2;
  const outerR = size / 2 - 2;
  const innerR = outerR * 0.55;
  const slices = buildDonutSlices(clusters, total, cx, cy, outerR, innerR);

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      {slices.map(s => (
        <path key={s.id} d={s.path} style={{ fill: `var(--theme-${(s.colorIndex % 8) + 1})` }} opacity={0.85} />
      ))}
      <text x={cx} y={cy - 6} textAnchor="middle" fontSize="20" fontWeight="700" fill="var(--color-ink)">{total}</text>
      <text x={cx} y={cy + 12} textAnchor="middle" fontSize="10" fill="var(--color-neutral-400)">documents</text>
    </svg>
  );
}

interface ProductionBriefProps {
  production: ProductionInfo;
  clusters: ClusterInfo[];
  activeClusterId: number | null;
  onSelectCluster: (id: number | null) => void;
  onViewDocument: (id: string) => void;
  onPipelineSettled?: () => void;
  onOpenEntity?: (id: string) => void;
}

export default function ProductionBrief({ production, clusters, activeClusterId, onSelectCluster, onViewDocument, onPipelineSettled, onOpenEntity }: ProductionBriefProps) {
  const [info, setInfo] = useState<PipelineInfo | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [collapsed, setCollapsed] = useState(() => safeGet(briefCollapseKey(production.id)) === '1');
  const [expanded, setExpanded] = useState(false);
  const [themeDocs, setThemeDocs] = useState<Record<number, ClusterDocument[]>>({});

  // Tracks which cluster ids we've already fetched key documents for, so the
  // expand effect doesn't refetch every time `clusters` gets a new reference
  // (Home refetches clusters on its own filter effect).
  const fetchedClusterIds = useRef<Set<number>>(new Set());

  // Detects a clustering-stage transition into "done" so the parent can be
  // told to refresh its (possibly stale) cluster list. Skipped on the very
  // first load — that's just discovering current state, not a transition.
  const firstLoadRef = useRef(true);
  const prevClusteringRef = useRef<PipelineStageState | undefined>(undefined);

  // Grace-period countdown (in poll ticks, ~5s each) covering the gap between
  // POST /pipeline/run enqueuing work and the worker's first status write.
  // Ticks down only while `starting` is true and no stage has been observed
  // running yet; reaching 0 gives up waiting and reveals whatever the server
  // actually reports (failed/retrofit/etc).
  const graceRef = useRef(0);

  const loadPipeline = useCallback(async () => {
    try {
      const data = await getPipeline(production.id);
      setInfo(data);
      if (anyStageRunning(data.status)) {
        // Real status now drives `isRunning` directly — no more need to
        // paper over enqueue latency.
        graceRef.current = 0;
        setStarting(false);
      } else {
        setStarting(prev => {
          if (!prev || graceRef.current <= 0) return false;
          graceRef.current -= 1;
          return graceRef.current > 0;
        });
      }
    } catch (e) {
      console.warn('getPipeline failed:', e);
    } finally {
      setIsLoading(false);
    }
  }, [production.id]);

  useEffect(() => {
    loadPipeline();
  }, [loadPipeline]);

  useEffect(() => {
    if (!info) return;
    const curr = info.status?.clustering;
    if (!firstLoadRef.current && prevClusteringRef.current !== 'done' && curr === 'done') {
      onPipelineSettled?.();
    }
    prevClusteringRef.current = curr;
    firstLoadRef.current = false;
  }, [info, onPipelineSettled]);

  const isRunning = starting || isActivelyRunning(info?.status);

  // Poll while the pipeline is running. Keyed on the derived boolean (not on
  // `info` itself) so a fresh 5s interval isn't torn down and rebuilt on
  // every single poll tick.
  useEffect(() => {
    if (!isRunning) return;
    const id = window.setInterval(() => { loadPipeline(); }, POLL_MS);
    return () => window.clearInterval(id);
  }, [isRunning, loadPipeline]);

  // Lazily fetch key documents per theme once the expansion is opened.
  useEffect(() => {
    if (!expanded) return;
    const toFetch = clusters.filter(c => !fetchedClusterIds.current.has(c.id));
    if (toFetch.length === 0) return;
    toFetch.forEach(c => fetchedClusterIds.current.add(c.id));

    let cancelled = false;
    Promise.all(
      toFetch.map(c =>
        getClusterDocuments(production.id, c.id)
          .then((docs): [number, ClusterDocument[]] => [c.id, docs])
          .catch((): [number, ClusterDocument[]] => [c.id, []]),
      ),
    ).then(results => {
      if (cancelled) return;
      setThemeDocs(prev => {
        const next = { ...prev };
        for (const [id, docs] of results) next[id] = docs;
        return next;
      });
    });
    return () => { cancelled = true; };
  }, [expanded, clusters, production.id]);

  const handleRunPipeline = useCallback(async (force = false) => {
    // ~30s grace period (6 ticks at the 5s poll interval) for the worker to
    // pick up the enqueued job and write its first status. `starting` is
    // cleared by `loadPipeline` once real status takes over (see above) —
    // never here — so a fast reload landing before the worker's first write
    // can't strand the UI on stale status.
    graceRef.current = 6;
    setStarting(true);
    try {
      await runPipeline(production.id, force);
      await loadPipeline();
    } catch (e) {
      showToast(`Could not start brief generation: ${e instanceof Error ? e.message : 'unknown error'}`, 'error');
    }
  }, [production.id, loadPipeline]);

  const toggleCollapsed = useCallback(() => {
    setCollapsed(prev => {
      const next = !prev;
      safeSet(briefCollapseKey(production.id), next ? '1' : '0');
      return next;
    });
  }, [production.id]);

  // ── State 1: loading — render nothing so there's no layout jump. ──
  if (isLoading) return null;

  // ── State 2: running — skeleton with per-stage glyphs. ──
  if (isRunning) {
    const status = info?.status;
    // During the grace window before any stage is actually observed running,
    // `status` may still be stale (e.g. a previous failed run) — show all
    // stages as pending rather than flashing a stale `!` glyph.
    const realRunning = isActivelyRunning(status);
    const glyph = (s: PipelineStageState | undefined) => (realRunning ? stageGlyph(s) : '·');
    // Per-document progress for the long summaries stage — only meaningful
    // while it is actually running (before that the counts reflect a prior run).
    const summariesProgress =
      realRunning && status?.summaries === 'running' && (info?.doc_count ?? 0) > 0
        ? ` ${info?.summarized_count ?? 0}/${info?.doc_count}`
        : '';
    return (
      <div className="brief-card brief-skeleton">
        <div className="brief-header">
          <span className="brief-ai-mark">✦</span>
          <span>AI is reading the production…</span>
        </div>
        <div className="brief-skeleton-bar" />
        <div className="brief-skeleton-bar" style={{ width: '70%' }} />
        <div className="brief-stages">
          <span>Clustering {glyph(status?.clustering)}</span>
          <span>Summaries{summariesProgress} {glyph(status?.summaries)}</span>
          <span>Brief {glyph(status?.brief)}</span>
        </div>
      </div>
    );
  }

  // ── State 3: brief present — the full card. ──
  if (info?.brief) {
    const brief = info.brief;
    const generatedDate = new Date(brief.generated_at).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
    });

    if (collapsed) {
      return (
        <div className="brief-card brief-collapsed">
          <button type="button" className="brief-collapse-toggle" onClick={toggleCollapsed} aria-expanded={false}>
            <span className="brief-ai-mark">✦</span> Production Brief <span aria-hidden="true">▸</span>
          </button>
          {clusters.length > 0 && (
            <div className="brief-chips">
              {clusters.map((c, i) => {
                // Unlabeled clusters (AI found no genuine common thread) get
                // no chip — a "Cluster N" chip implies a theme that isn't
                // there. Color index stays position-based so chips match the
                // document rows' badges.
                if (!c.label) return null;
                const isActive = activeClusterId === c.id;
                const dimmed = activeClusterId !== null && !isActive;
                return (
                  <button
                    key={c.id}
                    type="button"
                    className={`brief-chip${isActive ? ' is-active' : ''}${dimmed ? ' is-dimmed' : ''}`}
                    style={{ background: `var(--theme-${(i % 8) + 1})` }}
                    onClick={() => onSelectCluster(isActive ? null : c.id)}
                  >
                    {c.label}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      );
    }

    return (
      <div className="brief-card">
        <div className="brief-header">
          <h3 className="brief-title">
            <span className="brief-ai-mark">✦</span> Production Brief
          </h3>
          <span className="brief-generated-date">{generatedDate}</span>
          {production.is_owner && (
            <button
              type="button"
              className="btn-icon"
              onClick={() => handleRunPipeline(true)}
              disabled={starting}
              aria-label="Regenerate brief and themes"
              title="Regenerate brief and themes"
            >
              ↻
            </button>
          )}
          <button type="button" className="btn-icon" onClick={toggleCollapsed} aria-label="Collapse brief" aria-expanded={true}>
            ▾
          </button>
        </div>

        <p className="brief-overview">{brief.overview}</p>

        {(brief.key_players.length > 0 || brief.date_range) && (
          <div className="brief-meta">
            {info.key_players_resolved && info.key_players_resolved.length > 0 ? (
              <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
                {info.key_players_resolved.map((p, i) => (
                  p.entity_id ? (
                    <button
                      key={i}
                      type="button"
                      className="badge badge-gray"
                      style={{ cursor: 'pointer' }}
                      onClick={() => onOpenEntity?.(p.entity_id as string)}
                    >
                      {p.name}
                    </button>
                  ) : (
                    <span key={i} style={{ fontSize: 'var(--text-sm)' }}>{p.name}</span>
                  )
                ))}
              </span>
            ) : (
              brief.key_players.length > 0 && <span>{brief.key_players.join(', ')}</span>
            )}
            {brief.key_players.length > 0 && brief.date_range && <span> · </span>}
            {brief.date_range && <span>{brief.date_range}</span>}
          </div>
        )}

        {clusters.length > 0 && (
          <div className="brief-chips">
            {clusters.map((c, i) => {
              // Same rule as the collapsed state: no chip for clusters the
              // AI judged to have no genuine common thread.
              if (!c.label) return null;
              const isActive = activeClusterId === c.id;
              const dimmed = activeClusterId !== null && !isActive;
              return (
                <button
                  key={c.id}
                  type="button"
                  className={`brief-chip${isActive ? ' is-active' : ''}${dimmed ? ' is-dimmed' : ''}`}
                  style={{ background: `var(--theme-${(i % 8) + 1})` }}
                  onClick={() => onSelectCluster(isActive ? null : c.id)}
                >
                  {c.label}
                </button>
              );
            })}
          </div>
        )}

        {clusters.length > 0 && (
          <button
            type="button"
            className="brief-expand-toggle"
            onClick={() => setExpanded(prev => !prev)}
            aria-expanded={expanded}
          >
            Explore themes {expanded ? '▴' : '▾'}
          </button>
        )}

        {expanded && clusters.length > 0 && (
          <div className="brief-expand">
            <div className="brief-expand-donut">
              <ThemeDonut clusters={clusters} />
            </div>
            <div className="brief-expand-docs">
              {clusters.filter(c => c.label).map(c => (
                <div key={c.id} className="brief-theme-docs">
                  <div className="brief-theme-label">{c.label}</div>
                  {(themeDocs[c.id] ?? []).map(d => (
                    <button
                      key={d.document_id}
                      type="button"
                      className="brief-doc-row"
                      onClick={() => onViewDocument(d.document_id)}
                    >
                      <span className="brief-doc-title">{d.title || d.bates_begin}</span>
                      <span className="brief-doc-bates">{d.bates_begin}</span>
                    </button>
                  ))}
                  {themeDocs[c.id] && themeDocs[c.id].length === 0 && (
                    <div className="brief-doc-empty">No key documents</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {brief.notable_documents.length > 0 && (
          <div className="brief-notable">
            <div className="brief-notable-title">Notable documents</div>
            {brief.notable_documents.map((nd, i) => (
              <button
                key={i}
                type="button"
                className="brief-notable-row"
                onClick={async () => {
                  try {
                    const found = await getByBates(nd.bates, production.id);
                    onViewDocument(found.id);
                  } catch {
                    showToast(`Could not find ${nd.bates} in this production`, 'error');
                  }
                }}
              >
                <span className="brief-notable-bates">{nd.bates}</span>
                <span className="brief-notable-reason">{nd.reason}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  // ── State 4: no brief, a stage failed (or is stuck "running" past the
  // stale threshold) — owner sees Retry, others see nothing. ──
  if (anyStageFailed(info?.status) || (anyStageRunning(info?.status) && !isActivelyRunning(info?.status))) {
    if (!production.is_owner) return null;
    return (
      <div className="brief-card">
        <div className="brief-header">
          <h3 className="brief-title">
            <span className="brief-ai-mark">✦</span> Production Brief
          </h3>
        </div>
        <p className="brief-overview">Brief generation failed.</p>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => handleRunPipeline()} disabled={starting}>
          {starting ? 'Retrying…' : 'Retry'}
        </button>
      </div>
    );
  }

  // ── State 5: no brief, no status — the retrofit card (owner only). ──
  if (!info?.status) {
    if (!production.is_owner) return null;
    return (
      <div className="brief-card">
        <div className="brief-header">
          <h3 className="brief-title">
            <span className="brief-ai-mark">✦</span> Generate a Production Brief
          </h3>
        </div>
        <p className="brief-overview">AI will cluster, summarize, and brief this production.</p>
        <button type="button" className="btn btn-primary btn-sm" onClick={() => handleRunPipeline()} disabled={starting}>
          {starting ? 'Starting…' : 'Generate'}
        </button>
      </div>
    );
  }

  // Stages exist, none running/failed, but no brief yet (e.g. brief stage
  // hasn't been scheduled) — nothing sensible to show; the next poll or a
  // future mount will pick up the eventual brief or failure.
  return null;
}
