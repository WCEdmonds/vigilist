import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation } from 'd3-force';
import type { SimulationLinkDatum, SimulationNodeDatum } from 'd3-force';
import type { GraphEdge, GraphNode } from '../types';

export interface PositionedNode extends GraphNode {
  x: number;
  y: number;
  r: number;
}

interface SimNode extends GraphNode, SimulationNodeDatum {
  r: number;
}

interface SimLink extends SimulationLinkDatum<SimNode> {
  weight: number;
}

export function nodeRadius(mentionCount: number): number {
  return Math.max(8, Math.min(28, 6 + Math.sqrt(mentionCount) * 2));
}

/** Run the force simulation to convergence synchronously — calm first paint. */
export function computeGraphLayout(nodes: GraphNode[], edges: GraphEdge[], width: number, height: number): PositionedNode[] {
  if (nodes.length === 0) return [];
  const simNodes: SimNode[] = nodes.map(n => ({ ...n, r: nodeRadius(n.mention_count), x: 0, y: 0 }));
  if (simNodes.length === 1) return [{ ...simNodes[0], x: width / 2, y: height / 2 }];
  const simLinks: SimLink[] = edges.map(e => ({ source: e.source, target: e.target, weight: e.weight }));
  const sim = forceSimulation(simNodes)
    .force('charge', forceManyBody().strength(-180))
    .force('link', forceLink<SimNode, SimLink>(simLinks).id(d => d.id).distance(90))
    .force('collide', forceCollide<SimNode>().radius(d => d.r + 6))
    .force('center', forceCenter(width / 2, height / 2))
    .stop();
  for (let i = 0; i < 300; i++) sim.tick();
  return simNodes as unknown as PositionedNode[];
}
