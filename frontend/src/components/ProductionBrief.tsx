import { useCallback, useEffect, useRef, useState } from 'react';
import { getByBates, getPipeline, listEntities, runPipeline } from '../api/client';
import { entityDisplayName, isEntityNoise } from '../utils/entityDisplay';
import { showToast } from './Toast';
import type { EntityListItem, PipelineInfo, PipelineStageState, PipelineStatus, ProductionInfo } from '../types';

const POLL_MS = 5000;

// A worker killed mid-stage (e.g. Cloud Run scale-down) leaves a stage
// "running" forever with no further status writes. Mirrors the backend's
// STALE_RUNNING_MINUTES (backend/app/routers/productions.py) — the Cloud
// Tasks dispatch ceiling is 30 minutes, so 45 gives margin.
const STALE_RUNNING_MINUTES = 45;

const CAST_SIZE = 8;

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

interface CastMember {
  id: string;
  name: string;
  entityType: 'person' | 'org';
  mentionCount: number | null;
  /** Named a key player by the AI brief (not merely most-mentioned). */
  isKeyPlayer: boolean;
}


/**
 * The cast of characters: the brief's AI-designated key players first
 * (resolved to entities), then the most-mentioned entities that aren't
 * already listed, up to CAST_SIZE.
 */
function buildCast(
  keyPlayers: { name: string; entity_id: string | null }[] | null | undefined,
  topEntities: EntityListItem[],
): CastMember[] {
  const byId = new Map(topEntities.map(e => [e.id, e]));
  const cast: CastMember[] = [];
  const seen = new Set<string>();

  for (const kp of keyPlayers ?? []) {
    if (!kp.entity_id || seen.has(kp.entity_id)) continue;
    seen.add(kp.entity_id);
    const full = byId.get(kp.entity_id);
    const name = full?.canonical_name ?? kp.name;
    if (isEntityNoise(name)) continue;
    cast.push({
      id: kp.entity_id,
      name: entityDisplayName(name),
      entityType: full?.entity_type ?? 'person',
      mentionCount: full?.mention_count ?? null,
      isKeyPlayer: true,
    });
  }
  for (const e of topEntities) {
    if (cast.length >= CAST_SIZE) break;
    if (seen.has(e.id) || isEntityNoise(e.canonical_name)) continue;
    seen.add(e.id);
    cast.push({ id: e.id, name: entityDisplayName(e.canonical_name), entityType: e.entity_type, mentionCount: e.mention_count, isKeyPlayer: false });
  }
  return cast.slice(0, CAST_SIZE);
}

interface ProductionBriefProps {
  production: ProductionInfo;
  onViewDocument: (id: string) => void;
  onPipelineSettled?: () => void;
  onOpenEntity?: (id: string) => void;
  onOpenEntities?: () => void;
  onOpenGraph?: () => void;
  onOpenTimeline?: () => void;
}

export default function ProductionBrief({ production, onViewDocument, onPipelineSettled, onOpenEntity, onOpenEntities, onOpenGraph, onOpenTimeline }: ProductionBriefProps) {
  const [info, setInfo] = useState<PipelineInfo | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [collapsed, setCollapsed] = useState(() => safeGet(briefCollapseKey(production.id)) === '1');
  const [topEntities, setTopEntities] = useState<EntityListItem[]>([]);

  // Detects a pipeline transition into "done" so the parent can refresh.
  // Skipped on the very first load — that's just discovering current state.
  const firstLoadRef = useRef(true);
  const prevClusteringRef = useRef<PipelineStageState | undefined>(undefined);

  // Grace-period countdown (in poll ticks, ~5s each) covering the gap between
  // POST /pipeline/run enqueuing work and the worker's first status write.
  const graceRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    // Over-fetch: the noise filter (courts, reporters, clerks) thins the
    // list, and the cast should still fill out to CAST_SIZE afterwards.
    listEntities(production.id, undefined, undefined, 1, CAST_SIZE * 3)
      .then(p => { if (!cancelled) setTopEntities(p.entities); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [production.id]);

  const loadPipeline = useCallback(async () => {
    try {
      const data = await getPipeline(production.id);
      setInfo(data);
      if (anyStageRunning(data.status)) {
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
  // `info` itself) so a fresh 5s interval isn't torn down every poll tick.
  useEffect(() => {
    if (!isRunning) return;
    const id = window.setInterval(() => { loadPipeline(); }, POLL_MS);
    return () => window.clearInterval(id);
  }, [isRunning, loadPipeline]);

  const handleRunPipeline = useCallback(async (force = false) => {
    // ~30s grace period (6 ticks at the 5s poll interval) for the worker to
    // pick up the enqueued job and write its first status. `starting` is
    // cleared by `loadPipeline` once real status takes over — never here.
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

  const cast = buildCast(info?.key_players_resolved, topEntities);

  const castStrip = cast.length > 0 && (
    <div className="brief-cast">
      <div className="brief-cast-head">
        <span className="brief-cast-title">Cast of characters</span>
        <span className="brief-cast-links">
          <button type="button" onClick={onOpenEntities}>All entities</button>
          <button type="button" onClick={onOpenGraph}>Graph</button>
          <button type="button" onClick={onOpenTimeline}>Timeline</button>
        </span>
      </div>
      <div className="brief-cast-row">
        {cast.map(m => (
          <button key={m.id} type="button" className="cast-card" onClick={() => onOpenEntity?.(m.id)}
                  title={m.isKeyPlayer ? 'Named a key player in the brief' : undefined}>
            <span className={`entity-dot entity-${m.entityType}`}>●</span>
            <span className="cast-name">{m.name}</span>
            {m.isKeyPlayer && <span className="cast-key" aria-label="key player">✦</span>}
            {m.mentionCount !== null && <span className="cast-count">{m.mentionCount.toLocaleString()}</span>}
          </button>
        ))}
      </div>
    </div>
  );

  // ── State 1: loading — render nothing so there's no layout jump. ──
  if (isLoading) return null;

  // ── State 2: running — skeleton with per-stage glyphs. ──
  if (isRunning) {
    const status = info?.status;
    const realRunning = isActivelyRunning(status);
    const glyph = (s: PipelineStageState | undefined) => (realRunning ? stageGlyph(s) : '·');
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
          <span>Analysis {glyph(status?.clustering)}</span>
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
              aria-label="Regenerate brief"
              title="Regenerate brief"
            >
              ↻
            </button>
          )}
          <button type="button" className="btn-icon" onClick={toggleCollapsed} aria-label="Collapse brief" aria-expanded={true}>
            ▾
          </button>
        </div>

        <p className="brief-overview">{brief.overview}</p>

        {brief.date_range && (
          <div className="brief-meta">
            <span>{brief.date_range}</span>
          </div>
        )}

        {castStrip}

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
        <p className="brief-overview">AI will read, summarize, and brief this production.</p>
        <button type="button" className="btn btn-primary btn-sm" onClick={() => handleRunPipeline()} disabled={starting}>
          {starting ? 'Starting…' : 'Generate'}
        </button>
      </div>
    );
  }

  // Stages exist, none running/failed, but no brief yet — nothing sensible
  // to show; the next poll or a future mount will pick it up.
  return null;
}
