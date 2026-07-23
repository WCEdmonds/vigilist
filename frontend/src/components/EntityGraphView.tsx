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

const NODE_COLOR: Record<string, string> = { person: '#4f7cff', org: '#b4690e' };

export default function EntityGraphView({ productionId, openEntityId, onViewDocument, onBack, onOpenEntityChange }: Props) {
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [layout, setLayout] = useState<PositionedNode[]>([]);
  const [transform, setTransform] = useState<Transform>({ x: 0, y: 0, k: 1 });

  const containerRef = useRef<HTMLDivElement>(null);
  // Tracks the current pointer-drag target ('background' or a node id) across
  // pointer move events; read/written only inside pointer handlers, never
  // during render.
  const dragTargetRef = useRef<string | null>(null);

  const openEntity = (id: string | null) => { onOpenEntityChange?.(id); };

  useEffect(() => {
    getGraph(productionId)
      .then(data => {
        const rect = containerRef.current?.getBoundingClientRect();
        const width = rect?.width || 800;
        const height = rect?.height || 600;
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
    ev.currentTarget.setPointerCapture(ev.pointerId);
  };

  const onNodePointerDown = (id: string) => (ev: React.PointerEvent<SVGCircleElement>) => {
    ev.stopPropagation();
    dragTargetRef.current = id;
    ev.currentTarget.setPointerCapture(ev.pointerId);
  };

  const onPointerMove = (ev: React.PointerEvent<SVGSVGElement>) => {
    const target = dragTargetRef.current;
    if (!target) return;
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

  const edgeLine = (e: GraphEdge, i: number) => {
    const source = nodeById.get(e.source);
    const target = nodeById.get(e.target);
    if (!source || !target) return null;
    const stated = e.kind === 'stated';
    return (
      <line
        key={`${e.source}-${e.target}-${i}`}
        x1={source.x} y1={source.y} x2={target.x} y2={target.y}
        stroke="var(--color-border, #999)"
        strokeWidth={stated ? 1.5 : Math.min(4, e.weight / 2)}
        strokeOpacity={stated ? 0.7 : 0.25}
        strokeDasharray={stated ? undefined : '4 3'}
      >
        {e.relationship_type && <title>{e.relationship_type}</title>}
      </line>
    );
  };

  const showLabels = !(transform.k < 0.7 && layout.length > 40);

  return (
    <div style={{ position: 'relative', height: '100%', display: 'flex', flexDirection: 'column' }}>
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
                    fill={NODE_COLOR[n.entity_type] || NODE_COLOR.person}
                    stroke="#fff"
                    strokeWidth={1.5}
                    style={{ cursor: 'pointer' }}
                    onPointerDown={onNodePointerDown(n.id)}
                    onClick={() => openEntity(n.id)}
                  >
                    <title>{n.canonical_name}</title>
                  </circle>
                  {showLabels && (
                    <text
                      x={n.x + n.r + 4} y={n.y}
                      fontSize={11}
                      dominantBaseline="middle"
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
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
