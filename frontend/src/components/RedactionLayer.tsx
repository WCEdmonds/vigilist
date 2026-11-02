import { useState } from 'react';
import { REDACTION_REASON_LABELS } from '../types';
import type { Redaction, RedactionBox, RedactionReason } from '../types';

export type Box = { x: number; y: number; w: number; h: number };
type Corner = 'nw' | 'ne' | 'sw' | 'se';
// p = current pointer, s = pointer where the drag began (all page %).
type Drag =
  | { kind: 'draw'; sx: number; sy: number; px: number; py: number }
  | { kind: 'move'; id: number; sx: number; sy: number; px: number; py: number; orig: Box }
  | { kind: 'resize'; id: number; corner: Corner; px: number; py: number; orig: Box };

interface Props {
  pageNum: number;
  redactions: Redaction[];
  mode: 'view' | 'redact';
  reason: RedactionReason;
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  onCreate: (box: RedactionBox) => void;
  onUpdate: (id: number, box: Box) => void;
}

/** Strokes under this size (either way, % of the page) are stray clicks. */
const MIN_PCT = 0.5;
const CORNERS: Corner[] = ['nw', 'ne', 'sw', 'se'];

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

function spanning(x0: number, y0: number, x1: number, y1: number): Box {
  const x = clamp(Math.min(x0, x1), 0, 100);
  const y = clamp(Math.min(y0, y1), 0, 100);
  return { x, y, w: clamp(Math.max(x0, x1), 0, 100) - x, h: clamp(Math.max(y0, y1), 0, 100) - y };
}

function moveBox(orig: Box, dx: number, dy: number): Box {
  return { ...orig, x: clamp(orig.x + dx, 0, 100 - orig.w), y: clamp(orig.y + dy, 0, 100 - orig.h) };
}

function resizeBox(orig: Box, corner: Corner, px: number, py: number): Box {
  const west = corner === 'nw' || corner === 'sw';
  const north = corner === 'nw' || corner === 'ne';
  return spanning(west ? px : orig.x, north ? py : orig.y,
                  west ? orig.x + orig.w : px, north ? orig.y + orig.h : py);
}

/** The box a drag currently shows. */
function dragged(d: Drag): Box {
  if (d.kind === 'draw') return spanning(d.sx, d.sy, d.px, d.py);
  if (d.kind === 'move') return moveBox(d.orig, d.px - d.sx, d.py - d.sy);
  return resizeBox(d.orig, d.corner, d.px, d.py);
}

const boxOf = (r: Redaction): Box => ({ x: r.x_pct, y: r.y_pct, w: r.w_pct, h: r.h_pct });

/**
 * Redaction boxes over one page image, as an SVG in page percentages (so
 * zoom needs no maths). In redact mode: drag to draw, click to select, drag
 * a box to move it, drag a corner to resize. Saves on release.
 */
export default function RedactionLayer({ pageNum, redactions, mode, reason, selectedId, onSelect, onCreate, onUpdate }: Props) {
  const [drag, setDrag] = useState<Drag | null>(null);
  const editing = mode === 'redact';
  const pageBoxes = redactions.filter(r => r.page_num === pageNum);
  if (!editing && pageBoxes.length === 0) return null;

  const pointer = (e: React.PointerEvent): [number, number] => {
    const rect = (e.currentTarget as Element).closest('svg')!.getBoundingClientRect();
    return [((e.clientX - rect.left) / rect.width) * 100, ((e.clientY - rect.top) / rect.height) * 100];
  };

  const begin = (e: React.PointerEvent, next: (x: number, y: number) => Drag) => {
    if (!editing || e.button !== 0) return;
    e.stopPropagation();
    const [x, y] = pointer(e);
    (e.currentTarget as Element).closest('svg')!.setPointerCapture(e.pointerId);
    setDrag(next(x, y));
  };

  const onMove = (e: React.PointerEvent) => {
    if (!drag) return;
    const [px, py] = pointer(e);
    setDrag({ ...drag, px, py });
  };

  const onUp = () => {
    if (!drag) return;
    const d = drag;
    const b = dragged(d);
    setDrag(null);
    if (d.kind === 'draw') {
      if (b.w >= MIN_PCT && b.h >= MIN_PCT) {
        onCreate({ page_num: pageNum, x_pct: b.x, y_pct: b.y, w_pct: b.w, h_pct: b.h, reason_code: reason });
      }
    } else if (d.kind === 'move') {
      if (b.x !== d.orig.x || b.y !== d.orig.y) onUpdate(d.id, b);
    } else if (b.w >= MIN_PCT && b.h >= MIN_PCT) {
      onUpdate(d.id, b);
    }
  };

  const draft = drag?.kind === 'draw' ? dragged(drag) : null;

  return (
    <svg
      aria-label={`Redactions on page ${pageNum}`}
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', touchAction: 'none',
               pointerEvents: editing ? 'all' : 'none', cursor: editing ? 'crosshair' : 'default' }}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={() => setDrag(null)}
    >
      {editing && (
        <rect width="100%" height="100%" fill="transparent"
              onPointerDown={e => { onSelect(null); begin(e, (x, y) => ({ kind: 'draw', sx: x, sy: y, px: x, py: y })); }} />
      )}
      {pageBoxes.map(r => {
        const live = drag && drag.kind !== 'draw' && drag.id === r.id;
        const b = live ? dragged(drag) : boxOf(r);
        const selected = editing && r.id === selectedId;
        return (
          <g key={r.id}>
            <rect
              x={`${b.x}%`} y={`${b.y}%`} width={`${b.w}%`} height={`${b.h}%`}
              fill="rgba(0,0,0,0.55)"
              stroke={selected ? '#f6ad55' : 'rgba(0,0,0,0.9)'} strokeWidth={selected ? 2 : 1}
              style={{ cursor: editing ? 'move' : 'default' }}
              onPointerDown={e => {
                onSelect(r.id);
                begin(e, (x, y) => ({ kind: 'move', id: r.id, sx: x, sy: y, px: x, py: y, orig: boxOf(r) }));
              }}
            />
            <text x={`${b.x + b.w / 2}%`} y={`${b.y + b.h / 2}%`} fill="#fff" fontSize={11}
                  textAnchor="middle" dominantBaseline="middle" style={{ pointerEvents: 'none', userSelect: 'none' }}>
              {REDACTION_REASON_LABELS[r.reason_code] ?? r.reason_code}
            </text>
            {selected && CORNERS.map(c => (
              <rect key={c}
                x={`${c.endsWith('w') ? b.x : b.x + b.w}%`} y={`${c.startsWith('n') ? b.y : b.y + b.h}%`}
                width={10} height={10} transform="translate(-5,-5)" fill="#f6ad55" stroke="#fff"
                style={{ cursor: c === 'nw' || c === 'se' ? 'nwse-resize' : 'nesw-resize' }}
                onPointerDown={e => begin(e, (x, y) => ({ kind: 'resize', id: r.id, corner: c, px: x, py: y, orig: boxOf(r) }))}
              />
            ))}
          </g>
        );
      })}
      {draft && (
        <rect x={`${draft.x}%`} y={`${draft.y}%`} width={`${draft.w}%`} height={`${draft.h}%`}
              fill="rgba(0,0,0,0.35)" stroke="#f6ad55" strokeDasharray="4 3" />
      )}
    </svg>
  );
}
