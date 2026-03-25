import type { Annotation } from '../types';

const PIN_COLORS: Record<string, string> = {
  red: '#e53e3e',
  yellow: '#ecc94b',
  green: '#48bb78',
  blue: '#4299e1',
};

interface Props {
  annotations: Annotation[];
  pageNum: number;
  rotation: number;
  onPinClick: (annotation: Annotation, rect: DOMRect) => void;
  onPageClick: (pageNum: number, xPct: number, yPct: number, rect: DOMRect) => void;
}

export default function AnnotationOverlay({ annotations, pageNum, rotation, onPinClick, onPageClick }: Props) {
  if (rotation !== 0) return null;

  const pageAnnotations = annotations.filter(a => a.page_num === pageNum);

  const handleSvgClick = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = e.currentTarget;
    const rect = svg.getBoundingClientRect();
    const xPct = ((e.clientX - rect.left) / rect.width) * 100;
    const yPct = ((e.clientY - rect.top) / rect.height) * 100;
    onPageClick(pageNum, xPct, yPct, rect);
  };

  const handlePinClick = (e: React.MouseEvent, ann: Annotation) => {
    e.stopPropagation();
    const target = e.currentTarget as SVGGElement;
    const rect = target.getBoundingClientRect();
    onPinClick(ann, rect);
  };

  return (
    <svg
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        width: '100%',
        height: '100%',
        pointerEvents: 'none',
        cursor: 'crosshair',
      }}
      onClick={handleSvgClick}
    >
      <rect width="100%" height="100%" fill="transparent" style={{ pointerEvents: 'all', cursor: 'crosshair' }} />

      {pageAnnotations.map((ann, idx) => (
        <g key={ann.id} style={{ pointerEvents: 'all', cursor: 'pointer' }} onClick={(e) => handlePinClick(e, ann)}>
          <circle
            cx={`${ann.x_pct}%`}
            cy={`${ann.y_pct}%`}
            r={10}
            fill={PIN_COLORS[ann.color] || PIN_COLORS.blue}
            stroke="white"
            strokeWidth={2}
            opacity={0.9}
          />
          <text
            x={`${ann.x_pct}%`}
            y={`${ann.y_pct}%`}
            textAnchor="middle"
            dominantBaseline="central"
            fill="white"
            fontSize={10}
            fontWeight="bold"
            style={{ pointerEvents: 'none' }}
          >
            {idx + 1}
          </text>
        </g>
      ))}
    </svg>
  );
}
