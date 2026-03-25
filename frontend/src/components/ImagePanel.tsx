import { useCallback, useEffect, useRef, useState } from 'react';
import { imageUrl } from '../api/client';

interface Props {
  docId: string;
  pageCount: number;
}

export default function ImagePanel({ docId, pageCount }: Props) {
  const [zoom, setZoom] = useState(0.5);
  const [rotation, setRotation] = useState(0);
  const [vpWidth, setVpWidth] = useState(800);
  const viewportRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setZoom(0.5);
    setRotation(0);
    viewportRef.current?.scrollTo(0, 0);
  }, [docId]);

  // Track viewport width for zoom calculations
  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const ro = new ResizeObserver(entries => {
      for (const entry of entries) setVpWidth(entry.contentRect.width);
    });
    ro.observe(vp);
    return () => ro.disconnect();
  }, []);

  const handleWheel = useCallback((e: WheelEvent) => {
    if (e.ctrlKey) {
      e.preventDefault();
      setZoom(z => Math.max(0.25, Math.min(4, z - e.deltaY * 0.002)));
    }
  }, []);

  useEffect(() => {
    const vp = viewportRef.current;
    if (vp) vp.addEventListener('wheel', handleWheel, { passive: false });
    return () => { if (vp) vp.removeEventListener('wheel', handleWheel); };
  }, [handleWheel]);

  const imgWidth = vpWidth * zoom;

  return (
    <div className="viewer-main">
      {/* Toolbar */}
      <div className="image-toolbar">
        <span className="separator" />
        <button className="btn btn-secondary btn-sm" onClick={() => setZoom(z => Math.max(0.25, z - 0.25))}>−</button>
        <span className="page-info" style={{ minWidth: 40, textAlign: 'center' }}>{Math.round(zoom * 100)}%</span>
        <button className="btn btn-secondary btn-sm" onClick={() => setZoom(z => Math.min(4, z + 0.25))}>+</button>
        <button className="btn btn-secondary btn-sm" onClick={() => setZoom(0.5)}>Fit</button>
        <button className="btn btn-secondary btn-sm" onClick={() => setRotation(r => (r + 90) % 360)}>↻</button>
      </div>
      {/* All pages in scrollable viewport */}
      <div className="image-viewport" ref={viewportRef} style={{ flexDirection: 'column', alignItems: 'center', gap: 8 }}>
        {Array.from({ length: pageCount }, (_, i) => (
          <div key={i} style={{ position: 'relative', flexShrink: 0, width: 'fit-content' }}>
            <div style={{
              position: 'absolute', top: 4, left: 4, padding: '2px 8px',
              background: 'rgba(0,0,0,0.55)', color: '#fff', fontSize: 11,
              borderRadius: 4, zIndex: 1,
            }}>
              {i + 1}
            </div>
            <img
              src={imageUrl(docId, i + 1)}
              alt={`Page ${i + 1}`}
              style={{
                width: imgWidth,
                display: 'block',
                transform: rotation ? `rotate(${rotation}deg)` : undefined,
              }}
              draggable={false}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
