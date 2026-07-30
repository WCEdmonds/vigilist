import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchImageBlob } from '../api/client';
import type { Annotation } from '../types';
import AnnotationOverlay from './AnnotationOverlay';

interface Props {
  docId: string;
  pageCount: number;
  annotations?: Annotation[];
  onPinClick?: (annotation: Annotation, rect: DOMRect) => void;
  onPageClick?: (pageNum: number, xPct: number, yPct: number, rect: DOMRect) => void;
  onRotationChange?: (rotation: number) => void;
}

export default function ImagePanel({ docId, pageCount, annotations, onPinClick, onPageClick, onRotationChange }: Props) {
  const [zoom, setZoom] = useState(0.5);
  const [rotation, setRotation] = useState(0);
  const [vpWidth, setVpWidth] = useState(800);
  const viewportRef = useRef<HTMLDivElement>(null);
  const [blobUrls, setBlobUrls] = useState<Record<number, string>>({});

  useEffect(() => {
    setZoom(0.5);
    setRotation(0);
    setBlobUrls({});
    viewportRef.current?.scrollTo(0, 0);

    // Fetch page images as authenticated blobs, at 1200px, a few at a time.
    //
    // This used to fire one request per page simultaneously. On a long
    // document that meant ~120 concurrent requests, each holding a pooled DB
    // connection on the server while it downloaded and resized — which drained
    // the pool and failed the tail of them outright:
    //   QueuePool limit of size 5 overflow 10 reached, connection timed out
    // The server no longer holds a connection across that work, but there is
    // still no reason to ask for page 118 while the reader is on page 1.
    // Workers pull from a shared cursor, so pages arrive roughly top-down.
    let cancelled = false;
    const CONCURRENCY = 4;
    let nextPage = 1;

    const worker = async () => {
      for (;;) {
        const p = nextPage++;
        if (cancelled || p > pageCount) return;
        try {
          const url = await fetchImageBlob(docId, p, 1200);
          if (!cancelled) setBlobUrls(prev => ({ ...prev, [p]: url }));
        } catch (e) {
          if (!cancelled) console.warn(`fetchImageBlob page ${p} failed:`, e);
        }
      }
    };
    for (let i = 0; i < Math.min(CONCURRENCY, pageCount); i++) void worker();
    return () => {
      cancelled = true;
      // Revoke old blob URLs
      setBlobUrls(prev => {
        Object.values(prev).forEach(url => URL.revokeObjectURL(url));
        return {};
      });
    };
  }, [docId, pageCount]);

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
        <button className="btn btn-secondary btn-sm" onClick={() => { setRotation(r => { const next = (r + 90) % 360; onRotationChange?.(next); return next; }); }}>↻</button>
      </div>
      {/* All pages in scrollable viewport */}
      <div className="image-viewport" ref={viewportRef} style={{ flexDirection: 'column', alignItems: 'center', gap: 8 }}>
        {Array.from({ length: pageCount }, (_, i) => (
          <div key={i} id={`page-${i + 1}`} style={{ position: 'relative', flexShrink: 0, width: 'fit-content' }}>
            <div style={{
              position: 'absolute', top: 4, left: 4, padding: '2px 8px',
              background: 'rgba(44,62,107,0.65)', color: '#fff', fontSize: 11,
              borderRadius: 4, zIndex: 1,
            }}>
              {i + 1}
            </div>
            {blobUrls[i + 1] ? (
              <img
                src={blobUrls[i + 1]}
                alt={`Page ${i + 1}`}
                style={{
                  width: imgWidth,
                  display: 'block',
                  transform: rotation ? `rotate(${rotation}deg)` : undefined,
                }}
                draggable={false}
              />
            ) : (
              <div style={{ width: imgWidth, height: imgWidth * 1.4, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 'var(--space-2)', background: 'var(--color-neutral-100)', color: 'var(--color-neutral-500)', fontSize: 'var(--text-xs)' }}>
                <span className="spinner spinner-sm" /> Loading page {i + 1}…
              </div>
            )}
            <AnnotationOverlay
              annotations={annotations || []}
              pageNum={i + 1}
              rotation={rotation}
              onPinClick={(ann, rect) => onPinClick?.(ann, rect)}
              onPageClick={(pn, x, y, rect) => onPageClick?.(pn, x, y, rect)}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
