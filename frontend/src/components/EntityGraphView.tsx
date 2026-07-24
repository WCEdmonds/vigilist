import { useEffect, useRef, useState } from 'react';
import { getGraph } from '../api/client';
import type { GraphData, GraphEdge } from '../types';
import { computeGraphLayout, type PositionedNode } from '../utils/graphLayout';
import EntityPanel from './EntityPanel';

interface Props {
  productionId: number;
  openEntityId?: string | null;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
  onOpenEntityChange?: (id: string | null) => void;
}

interface Transform {
  x: number;
  y: number;
  k: number;
}

// Record language (§000004): white nodes on the redaction-black band,
// typed by ring color — person = stamp blue, org = marker gold.
const NODE_RING: Record<string, string> = { person: '#2f3dbd', org: '#f5ce00' };

export default function EntityGraphView({ productionId, openEntityId, onViewDocument, onBack, onOpenEntityChange }: Props) {
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [layout, setLayout] = useState<PositionedNode[]>([]);
  const [transform, setTransform] = useState<Transform>({ x: 0, y: 0, k: 1 });

  const containerRef = useRef<HTMLDivElement>(null);
  // Tracks the current pointer-drag target ('background' or a node id) across
  // pointer move events; read/written only inside pointer handlers, never
  // during render.
  const dragTargetRef = useRef<string | null>(null);
  // Cumulative pointer movement (px) since the last pointerdown. A pointerup
  // on a node always fires a trailing click event; without this a drag-release
  // would reopen the entity panel on every reposition. Standard "click
  // distance" pattern: only treat it as a real click if movement stayed small.
  const dragDistanceRef = useRef(0);
  const CLICK_DISTANCE_THRESHOLD = 4;

  const openEntity = (id: string | null) => { onOpenEntityChange?.(id); };

  useEffect(() => {
    getGraph(productionId)
      .then(data => {
        const rect = containerRef.current?.getBoundingClientRect();
        // If the container hasn't laid out yet (rect reads 0-height), fall
        // back to the real viewport rather than an arbitrary guess so nodes
        // don't land off-screen on a full-height view.
        const HEADER_OFFSET = 48;
        const width = rect?.width || window.innerWidth;
        const height = rect?.height || (window.innerHeight - HEADER_OFFSET);
        setLayout(computeGraphLayout(data.nodes, data.edges, width, height));
        setGraphData(data);
      })
      .catch(e => console.warn('getGraph failed:', e));
  }, [productionId]);

  const nodeById = new Map(layout.map(n => [n.id, n]));

  const onWheel = (ev: React.WheelEvent<SVGSVGElement>) => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
    setTransform(t => ({ ...t, k: Math.max(0.3, Math.min(3, t.k * factor)) }));
  };

  const onBackgroundPointerDown = (ev: React.PointerEvent<SVGSVGElement>) => {
    dragTargetRef.current = 'background';
    dragDistanceRef.current = 0;
    ev.currentTarget.setPointerCapture(ev.pointerId);
  };

  const onNodePointerDown = (id: string) => (ev: React.PointerEvent<SVGCircleElement>) => {
    ev.stopPropagation();
    dragTargetRef.current = id;
    dragDistanceRef.current = 0;
    ev.currentTarget.setPointerCapture(ev.pointerId);
  };

  const onPointerMove = (ev: React.PointerEvent<SVGSVGElement>) => {
    const target = dragTargetRef.current;
    if (!target) return;
    dragDistanceRef.current += Math.hypot(ev.movementX, ev.movementY);
    if (target === 'background') {
      setTransform(t => ({ ...t, x: t.x + ev.movementX, y: t.y + ev.movementY }));
    } else {
      const k = transform.k;
      setLayout(prev => prev.map(n => (
        n.id === target ? { ...n, x: n.x + ev.movementX / k, y: n.y + ev.movementY / k } : n
      )));
    }
  };

  const onPointerUp = () => { dragTargetRef.current = null; };

  const onNodeClick = (id: string) => () => {
    // A pointerup always fires a trailing click; only open the panel if this
    // pointer session didn't actually drag the node.
    const dragged = dragDistanceRef.current > CLICK_DISTANCE_THRESHOLD;
    dragDistanceRef.current = 0;
    if (dragged) return;
    openEntity(id);
  };

  const edgeLine = (e: GraphEdge, i: number) => {
    const source = nodeById.get(e.source);
    const target = nodeById.get(e.target);
    if (!source || !target) return null;
    const stated = e.kind === 'stated';
    return (
      <line
        key={`${e.source}-${e.target}-${i}`}
        x1={source.x} y1={source.y} x2={target.x} y2={target.y}
        stroke="#ffffff"
        strokeWidth={stated ? 1.5 : Math.min(4, e.weight / 2)}
        strokeOpacity={stated ? 0.55 : 0.22}
        strokeDasharray={stated ? undefined : '4 3'}
      >
        {e.relationship_type && <title>{e.relationship_type}</title>}
      </line>
    );
  };

  const showLabels = !(transform.k < 0.7 && layout.length > 40);

  return (
    <div style={{ position: 'relative', height: '100dvh', display: 'flex', flexDirection: 'column' }}>
      <div className="panel-header" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn btn-ghost btn-xs" onClick={onBack}>← Back</button>
        <span style={{ fontWeight: 600 }}>Relationship Graph ({layout.length} entities)</span>
        {graphData?.truncated && (
          <span style={{ fontSize: 'var(--text-xs)', opacity: 0.7 }}>
            Showing top {layout.length} entities by mentions.
          </span>
        )}
      </div>

      <div ref={containerRef} style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
        {graphData && graphData.nodes.length === 0 && (
          <div className="empty-state">
            No entity relationships extracted yet — run entity extraction from the Entities view.
          </div>
        )}

        {graphData && graphData.nodes.length > 0 && (
          <svg
            className="egraph"
            style={{ width: '100%', height: '100%', touchAction: 'none' }}
            onWheel={onWheel}
            onPointerDown={onBackgroundPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerLeave={onPointerUp}
          >
            <g transform={`translate(${transform.x},${transform.y}) scale(${transform.k})`}>
              {graphData.edges.map(edgeLine)}
              {layout.map(n => (
                <g key={n.id}>
                  <circle
                    cx={n.x} cy={n.y} r={n.r}
                    fill="#ffffff"
                    stroke={NODE_RING[n.entity_type] || NODE_RING.person}
                    strokeWidth={2}
                    style={{ cursor: 'pointer' }}
                    onPointerDown={onNodePointerDown(n.id)}
                    onClick={onNodeClick(n.id)}
                  >
                    <title>{n.canonical_name}</title>
                  </circle>
                  {showLabels && (
                    <text
                      x={n.x + n.r + 5} y={n.y}
                      fontSize={10.5}
                      dominantBaseline="middle"
                      style={{ pointerEvents: 'none', userSelect: 'none', fill: 'rgba(255,255,255,0.85)', fontFamily: 'var(--font-mono)' }}
                    >
                      {n.canonical_name}
                    </text>
                  )}
                </g>
              ))}
            </g>
          </svg>
        )}
      </div>

      {openEntityId && (
        <EntityPanel entityId={openEntityId} onClose={() => openEntity(null)}
                     onOpenEntity={openEntity}
                     onOpenDocument={docId => { openEntity(null); onViewDocument(docId); }} />
      )}
    </div>
  );
}
