import { Fragment, useCallback, useEffect, useMemo, useRef, type ReactNode } from 'react';
import type { DocEntity } from '../types';

interface Props {
  text: string | null;
  searchQuery?: string;
  entities?: DocEntity[];
  onEntityClick?: (entityId: string) => void;
  focusEntityId?: string | null;
  onTitleChanged?: (title: string) => void;
}

function highlightTerms(text: string, searchQuery?: string): ReactNode {
  if (!searchQuery) return text;
  const terms = searchQuery
    .replace(/["()]/g, '')
    .split(/\s+/)
    .filter(t => t && !['AND', 'OR', 'NOT'].includes(t.toUpperCase()));
  if (terms.length === 0) return text;

  const escaped = terms.map(t =>
    t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\*$/, '\\w*'),
  );
  const regex = new RegExp(`(${escaped.join('|')})`, 'gi');
  const parts = text.split(regex);
  return parts.map((part, i) => {
    if (i % 2 === 1) return <mark key={i}>{part}</mark>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}

interface EntitySpan {
  start: number;
  end: number;
  entityId: string;
  entityType: string;
  name: string;
}

/** Flatten entity mentions into a non-overlapping, offset-sorted span list. */
function buildSpans(entities: DocEntity[]): EntitySpan[] {
  const spans: EntitySpan[] = [];
  for (const e of entities) {
    for (const m of e.mentions) {
      if (m.start_offset == null || m.end_offset == null) continue;
      spans.push({ start: m.start_offset, end: m.end_offset, entityId: e.id, entityType: e.entity_type, name: e.canonical_name });
    }
  }
  spans.sort((a, b) => a.start - b.start);
  const out: EntitySpan[] = [];
  let lastEnd = -1;
  for (const s of spans) {
    if (s.start < lastEnd) continue; // drop overlaps
    out.push(s);
    lastEnd = s.end;
  }
  return out;
}

function renderWithEntities(
  text: string,
  spans: EntitySpan[],
  searchQuery: string | undefined,
  onEntityClick?: (entityId: string) => void,
): ReactNode {
  if (spans.length === 0) return highlightTerms(text, searchQuery);
  const parts: ReactNode[] = [];
  let cursor = 0;
  spans.forEach((s, i) => {
    if (s.start > cursor) {
      parts.push(<Fragment key={`t${i}`}>{highlightTerms(text.slice(cursor, s.start), searchQuery)}</Fragment>);
    }
    parts.push(
      <mark
        key={`e${i}`}
        className={`entity-mark entity-${s.entityType}`}
        data-entity-id={s.entityId}
        role="button"
        tabIndex={0}
        title={s.name}
        style={{ cursor: 'pointer' }}
        onClick={() => onEntityClick?.(s.entityId)}
        onKeyDown={ev => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            onEntityClick?.(s.entityId);
          }
        }}
      >
        {text.slice(s.start, s.end)}
      </mark>,
    );
    cursor = s.end;
  });
  if (cursor < text.length) {
    parts.push(<Fragment key="tail">{highlightTerms(text.slice(cursor), searchQuery)}</Fragment>);
  }
  return parts;
}

export default function TextPanel({ text, searchQuery, entities, onEntityClick, focusEntityId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  const copyToClipboard = useCallback(() => {
    if (text) navigator.clipboard.writeText(text);
  }, [text]);

  const rendered = useMemo(() => {
    if (!text) return null;
    const spans = entities?.length ? buildSpans(entities) : [];
    return renderWithEntities(text, spans, searchQuery, onEntityClick);
  }, [text, searchQuery, entities, onEntityClick]);

  useEffect(() => {
    if (!focusEntityId || !containerRef.current) return;
    const el = containerRef.current.querySelector(`[data-entity-id="${focusEntityId}"]`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [focusEntityId, entities]);

  if (!text) {
    return <div className="empty-state">No extracted text available</div>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="panel-header">
        <span>Extracted Text</span>
        <button onClick={copyToClipboard} className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }} aria-label="Copy extracted text to clipboard">
          Copy
        </button>
      </div>
      <div
        ref={containerRef}
        style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)', fontSize: 'var(--text-sm)', lineHeight: 1.65, whiteSpace: 'pre-wrap', fontFamily: 'var(--font-mono)' }}
      >
        {rendered}
      </div>
    </div>
  );
}
