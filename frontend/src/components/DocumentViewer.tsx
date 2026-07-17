import { useCallback, useEffect, useRef, useState } from 'react';
import { createAnnotation, deleteAnnotation, findSimilar, getDocument, getDocumentDuplicates, getDocumentFamily, getDocumentNav, listAnnotations, summarizeDocument, updateAnnotation } from '../api/client';
import type { Annotation, DocumentDetail, DocumentTagEntry, DuplicateEntry, FamilyMember, FamilyThread } from '../types';
import DocumentNav from './DocumentNav';
import ImagePanel from './ImagePanel';
import NativeViewer, { type MediaPlayerHandle } from './NativeViewer';
import MetadataPanel from './MetadataPanel';
import NotesPanel from './NotesPanel';
import TagBar from './TagBar';
import TextPanel from './TextPanel';
import AnnotationPopover from './AnnotationPopover';
import AnnotationSidebar from './AnnotationSidebar';

const TIER_RANK: Record<string, number> = { hash: 0, exact: 1, similar: 2 };
const tierRank = (t: string): number => TIER_RANK[t] ?? 9;

const FamilyList = ({ label, items, onNavigate }: {
  label: string; items: FamilyMember[]; onNavigate: (id: string) => void;
}) => {
  if (items.length === 0) return null;
  return (
    <div style={{ flex: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', borderTop: '1px solid rgba(44,62,107,0.08)' }}>
      <div className="panel-header">{label} ({items.length})</div>
      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--space-2)' }}>
        {items.map(m => (
          <div key={m.document_id} onClick={() => onNavigate(m.document_id)}
            style={{ padding: 'var(--space-1-5)', cursor: 'pointer', fontSize: 'var(--text-xs)', borderBottom: '1px solid rgba(44,62,107,0.06)' }}>
            <div style={{ fontWeight: 600 }}>{m.bates_begin}</div>
            <div style={{ color: 'rgba(44,62,107,0.5)' }}>{m.title || 'No title'}</div>
            {m.is_inclusive && <span className="badge badge-gray" style={{ fontSize: 9 }}>Inclusive</span>}
          </div>
        ))}
      </div>
    </div>
  );
};

interface Props {
  docId: string;
  onNavigate: (id: string) => void;
  onBack: () => void;
  searchQuery?: string;
  onSearch?: (query: string) => void;
  onSimilarResults?: (label: string, results: import('../types').SearchResult[]) => void;
  docIds?: string[];
}

type RightTab = 'text' | 'metadata' | 'summary';
type CenterTab = 'images' | 'native';

export default function DocumentViewer({ docId, onNavigate, onBack, searchQuery, onSearch, onSimilarResults, docIds }: Props) {
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState('');
  const [rightTab, setRightTab] = useState<RightTab>('text');
  const [centerTab, setCenterTab] = useState<CenterTab>('images');
  const [nextId, setNextId] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [similarLoading, setSimilarLoading] = useState(false);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [duplicates, setDuplicates] = useState<DuplicateEntry[]>([]);
  const [family, setFamily] = useState<FamilyThread>({ family: [], thread: [] });
  const [imageRotation, setImageRotation] = useState(0);
  const [mediaTime, setMediaTime] = useState<number | null>(null);
  const mediaRef = useRef<MediaPlayerHandle>(null);
  const [mobileTab, setMobileTab] = useState<'view' | 'notes' | 'text'>('view');
  const [isMobile] = useState(() => typeof window !== 'undefined' && window.innerWidth < 768);
  const [popover, setPopover] = useState<{
    mode: 'color-picker' | 'create' | 'view';
    position: { top: number; left: number };
    annotation?: Annotation;
    pendingPin?: { pageNum: number; xPct: number; yPct: number };
    selectedColor?: string;
  } | null>(null);

  useEffect(() => {
    setError('');
    setSummary(null);
    setCenterTab('images');
    setAnnotations([]);
    setDuplicates([]);
    setFamily({ family: [], thread: [] });
    setPopover(null);
    getDocument(docId).then(d => {
      setDoc(d);
      if (d.summary) setSummary(d.summary);
    }).catch(e => setError(e.message));
    getDocumentNav(docId).then(nav => setNextId(nav.next_id)).catch(e => console.warn('getDocumentNav failed:', e));
    listAnnotations(docId).then(setAnnotations).catch(e => console.warn('listAnnotations failed:', e));
    getDocumentDuplicates(docId).then(setDuplicates).catch(e => console.warn('getDocumentDuplicates failed:', e));
    getDocumentFamily(docId).then(setFamily).catch(e => console.warn('getDocumentFamily failed:', e));
  }, [docId]);

  const handleTagsChanged = useCallback((tags: DocumentTagEntry[]) => {
    if (doc) setDoc({ ...doc, tags });
  }, [doc]);

  const handleDownload = async () => {
    if (!doc) return;
    try {
      if (doc.native_path) {
        const { getNativeUrl } = await import('../api/client');
        const { url, filename } = await getNativeUrl(doc.id, true);
        const a = document.createElement('a');
        a.href = url; a.download = filename; a.click();
      } else if (doc.image_paths.length > 0) {
        const { fetchDocumentPdf } = await import('../api/client');
        const blob = await fetchDocumentPdf(doc.id);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = `${doc.bates_begin}.pdf`; a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } else {
        window.alert('Nothing to download — this document has no native file or page images.');
      }
    } catch (e: unknown) {
      window.alert(`Download failed: ${e instanceof Error ? e.message : 'unknown error'}`);
    }
  };

  const handleAutoAdvance = useCallback(() => {
    if (nextId) onNavigate(nextId);
  }, [nextId, onNavigate]);

  const handleSummarize = async () => {
    setSummaryLoading(true);
    setRightTab('summary');
    try {
      const res = await summarizeDocument(docId);
      setSummary(res.summary);
      if (doc) setDoc({ ...doc, summary: res.summary });
    } catch (e: unknown) {
      setSummary(`Error: ${e instanceof Error ? e.message : 'unknown error'}`);
    } finally {
      setSummaryLoading(false);
    }
  };

  const handleFindSimilar = async () => {
    setSimilarLoading(true);
    try {
      const res = await findSimilar(docId);
      if (onSimilarResults && res.results?.length > 0) {
        // Backend returned actual similarity results — show them directly
        onSimilarResults(`Similar to ${doc?.bates_begin || 'document'}`, res.results);
      } else if (onSearch) {
        // Fallback: search with extracted terms
        onSearch(res.search_terms);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'An error occurred');
    } finally {
      setSimilarLoading(false);
    }
  };

  const handlePageClick = (pageNum: number, xPct: number, yPct: number, rect: DOMRect) => {
    setPopover({
      mode: 'color-picker',
      position: { top: rect.top + (yPct / 100) * rect.height, left: rect.left + (xPct / 100) * rect.width + 16 },
      pendingPin: { pageNum, xPct, yPct },
    });
  };

  const handleColorSelect = async (color: string) => {
    if (!popover?.pendingPin || !doc) return;
    const { pageNum, xPct, yPct } = popover.pendingPin;
    try {
      const ann = await createAnnotation(doc.id, pageNum, xPct, yPct, color);
      setAnnotations(prev => [...prev, ann]);
      setPopover({
        mode: 'create',
        position: popover.position,
        annotation: ann,
        selectedColor: color,
      });
    } catch {
      setPopover(null);
    }
  };

  const handleAnnotationSave = async (content: string) => {
    if (!popover?.annotation) { setPopover(null); return; }
    if (content) {
      try {
        const updated = await updateAnnotation(popover.annotation.id, { content });
        setAnnotations(prev => prev.map(a => a.id === updated.id ? updated : a));
      } catch { /* pin stays without content */ }
    }
    setPopover(null);
  };

  const handlePinClick = (ann: Annotation, rect: DOMRect) => {
    setPopover({
      mode: 'view',
      position: { top: rect.top, left: rect.right + 8 },
      annotation: ann,
    });
  };

  const handleAnnotationUpdate = async (data: { content?: string; color?: string }) => {
    if (!popover?.annotation) return;
    try {
      const updated = await updateAnnotation(popover.annotation.id, data);
      setAnnotations(prev => prev.map(a => a.id === updated.id ? updated : a));
      setPopover(null);
    } catch { /* ignore */ }
  };

  const handleAnnotationDelete = async () => {
    if (!popover?.annotation) return;
    try {
      await deleteAnnotation(popover.annotation.id);
      setAnnotations(prev => prev.filter(a => a.id !== popover.annotation!.id));
      setPopover(null);
    } catch { /* ignore */ }
  };

  const handleSidebarSelect = (ann: Annotation) => {
    const el = document.getElementById(`page-${ann.page_num}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };

  const hasNative = !!doc?.native_path;
  const hasImages = (doc?.page_count ?? 0) > 0 && (doc?.image_paths?.length ?? 0) > 0;

  // Auto-default to native tab when doc has native but no real images
  useEffect(() => {
    if (hasNative && !hasImages) setCenterTab('native');
    else setCenterTab('images');
  }, [docId, hasNative, hasImages]);

  if (error) return <div style={{ padding: 32, color: 'var(--color-danger-600)' }}>Error: {error}</div>;
  if (!doc) return (
    <div className="loading-fullscreen">
      <span className="spinner" />
      <div>Loading document…</div>
    </div>
  );

  const showCenterTabs = hasNative && hasImages;

  const isProcessing = doc.processing_status !== 'complete';

  const renderCenterPanel = () => {
    // Native tab selected, or native-only doc
    if (hasNative && (centerTab === 'native' || !hasImages)) {
      return <NativeViewer ref={mediaRef} docId={doc.id} nativePath={doc.native_path!} onTimeUpdate={setMediaTime} />;
    }

    // Images still being converted (Phase B pending)
    if (isProcessing && doc.image_paths.length === 0) {
      return (
        <div className="viewer-main">
          <div className="empty-state" style={{ flex: 1, gap: 'var(--space-2)' }}>
            <span className="spinner spinner-md" />
            <div style={{ fontSize: 'var(--text-lg)', fontFamily: 'var(--font-serif)' }}>Images Processing</div>
            <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-400)' }}>
              Document text and metadata are available. Page images are being converted and will appear automatically.
            </div>
          </div>
        </div>
      );
    }

    // Has images (default)
    if (hasImages) {
      return <ImagePanel docId={doc.id} pageCount={doc.page_count} annotations={annotations} onPinClick={handlePinClick} onPageClick={handlePageClick} onRotationChange={setImageRotation} />;
    }

    return (
      <div className="viewer-main">
        <div className="empty-state" style={{ flex: 1 }}>
          <div>No viewable content for this document.</div>
        </div>
      </div>
    );
  };

  if (isMobile) {
    // Find prev/next from docIds
    const curIdx = docIds?.indexOf(doc.id) ?? -1;
    const mobilePrev = curIdx > 0 ? docIds![curIdx - 1] : null;
    const mobileNext = curIdx >= 0 && curIdx < (docIds?.length ?? 0) - 1 ? docIds![curIdx + 1] : null;

    const sheetOpen = mobileTab !== 'view';

    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh', background: 'var(--color-neutral-50)', position: 'relative', overflow: 'hidden' }}>
        {/* Compact top bar */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: '8px 12px', background: 'var(--color-ink)', color: '#fff', flexShrink: 0, zIndex: 10 }}>
          <button onClick={onBack} style={{ background: 'none', border: 'none', color: '#fff', fontSize: 16, cursor: 'pointer', padding: '4px' }}>←</button>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{doc.bates_begin}</div>
            {doc.title && <div style={{ fontSize: 11, opacity: 0.6, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{doc.title}</div>}
          </div>
          <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
            <button
              onClick={handleDownload}
              aria-label="Download"
              title="Download"
              style={{ background: 'rgba(255,255,255,0.15)', border: 'none', color: '#fff', borderRadius: 4, padding: '6px 10px', cursor: 'pointer', fontSize: 14, lineHeight: 1 }}
            >
              ↓
            </button>
            <button disabled={!mobilePrev} onClick={() => mobilePrev && onNavigate(mobilePrev)} style={{ background: 'rgba(255,255,255,0.15)', border: 'none', color: '#fff', borderRadius: 4, padding: '6px 10px', cursor: 'pointer', opacity: mobilePrev ? 1 : 0.3 }}>←</button>
            {docIds && curIdx >= 0 && <span style={{ fontSize: 11, opacity: 0.5, alignSelf: 'center' }}>{curIdx + 1}/{docIds.length}</span>}
            <button disabled={!mobileNext} onClick={() => mobileNext && onNavigate(mobileNext)} style={{ background: 'rgba(255,255,255,0.15)', border: 'none', color: '#fff', borderRadius: 4, padding: '6px 10px', cursor: 'pointer', opacity: mobileNext ? 1 : 0.3 }}>→</button>
          </div>
        </div>

        {/* Document always visible behind sheets */}
        <div style={{ flex: 1, overflow: 'auto' }}>
          {renderCenterPanel()}
        </div>

        {/* Bottom sheet overlay */}
        {sheetOpen && (
          <div
            onClick={() => setMobileTab('view')}
            style={{ position: 'absolute', inset: 0, top: 44, background: 'rgba(0,0,0,0.3)', zIndex: 20 }}
          />
        )}

        {/* Bottom sheet */}
        <div style={{
          position: 'absolute', left: 0, right: 0, bottom: 0, zIndex: 30,
          transform: sheetOpen ? 'translateY(0)' : 'translateY(calc(100% - 48px))',
          transition: 'transform 0.25s ease',
          display: 'flex', flexDirection: 'column',
          maxHeight: '70dvh',
          background: '#fff',
          borderRadius: '16px 16px 0 0',
          boxShadow: '0 -4px 24px rgba(0,0,0,0.12)',
        }}>
          {/* Tab bar (always visible as the sheet handle) */}
          <div style={{ display: 'flex', flexShrink: 0, borderBottom: sheetOpen ? '1px solid rgba(44,62,107,0.1)' : 'none' }}>
            {/* Drag handle */}
            <div style={{ position: 'absolute', top: 8, left: '50%', transform: 'translateX(-50%)', width: 32, height: 4, borderRadius: 2, background: 'rgba(44,62,107,0.15)' }} />
            {(['notes', 'text'] as const).map(tab => (
              <button
                key={tab}
                onClick={() => setMobileTab(mobileTab === tab ? 'view' : tab)}
                style={{
                  flex: 1, padding: '14px 0 10px', border: 'none', cursor: 'pointer',
                  background: 'transparent',
                  color: mobileTab === tab ? 'var(--color-ink)' : 'rgba(44,62,107,0.4)',
                  fontWeight: mobileTab === tab ? 700 : 500,
                  fontSize: 12,
                }}
              >
                {tab === 'notes' ? 'Notes & Tags' : 'Text'}
              </button>
            ))}
          </div>

          {/* Sheet content */}
          {sheetOpen && (
            <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
              {mobileTab === 'notes' && (
                <>
                  <div style={{ flexShrink: 0 }}>
                    <TagBar docId={doc.id} tags={doc.tags} onTagsChanged={handleTagsChanged} onAutoAdvance={handleAutoAdvance} />
                  </div>
                  <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                    <NotesPanel docId={doc.id} mediaTime={mediaTime} onSeek={(t) => mediaRef.current?.seekTo(t)} />
                  </div>
                </>
              )}
              {mobileTab === 'text' && (
                <div style={{ flex: 1, overflow: 'auto' }}>
                  <TextPanel text={doc.text_content} searchQuery={searchQuery} />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      {/* Header */}
      <div className="app-header">
        <button className="btn-header" onClick={onBack}>← Back</button>
        <span className="logo">Vigilist</span>
        <div className="user-menu">
          <button className="btn-header" onClick={handleDownload}>Download File</button>
        </div>
      </div>

      {/* Nav bar */}
      <DocumentNav doc={doc} onNavigate={onNavigate} searchQuery={searchQuery} onTitleChanged={(title) => setDoc(prev => prev ? { ...prev, title } : prev)} docIds={docIds} />

      {/* Main content — three-column layout */}
      <div className="viewer-layout">
        {/* LEFT SIDEBAR — Actions */}
        <div className="viewer-left-sidebar">
          {/* Tags */}
          <div className="sidebar-section">
            <TagBar docId={doc.id} tags={doc.tags} onTagsChanged={handleTagsChanged} onAutoAdvance={handleAutoAdvance} />
          </div>

          {/* Notes & Pins — split evenly */}
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, borderBottom: doc.page_count > 0 ? '1px solid rgba(44,62,107,0.08)' : undefined }}>
              <div className="panel-header">Notes</div>
              <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column' }}>
                <NotesPanel docId={doc.id} mediaTime={mediaTime} onSeek={(t) => mediaRef.current?.seekTo(t)} />
              </div>
            </div>

            {doc.page_count > 0 && (
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                <div className="panel-header">Pins{annotations.length > 0 ? ` (${annotations.length})` : ''}</div>
                <div style={{ flex: 1, overflowY: 'auto' }}>
                  <AnnotationSidebar
                    annotations={annotations}
                    rotation={imageRotation}
                    pageCount={doc.page_count}
                    onSelect={handleSidebarSelect}
                  />
                </div>
              </div>
            )}

            {duplicates.length > 0 && (
              <div style={{ flex: 0, minHeight: 60, display: 'flex', flexDirection: 'column', overflow: 'hidden', borderTop: '1px solid rgba(44,62,107,0.08)' }}>
                <div className="panel-header">Duplicates ({duplicates.length})</div>
                <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--space-2)' }}>
                  {[...duplicates].sort((a, b) => tierRank(a.type) - tierRank(b.type)).map(d => (
                    <div
                      key={d.document_id}
                      onClick={() => onNavigate(d.document_id)}
                      style={{ padding: 'var(--space-1-5)', cursor: 'pointer', fontSize: 'var(--text-xs)', borderBottom: '1px solid rgba(44,62,107,0.06)' }}
                    >
                      <div style={{ fontWeight: 600 }}>{d.bates_begin}</div>
                      <div style={{ color: 'rgba(44,62,107,0.5)' }}>{d.title || 'No title'}</div>
                      <span className="badge badge-gray" style={{ fontSize: 9 }}>
                        {d.type === 'hash'
                          ? 'Identical file'
                          : d.type === 'exact'
                            ? `Near-identical text · ${Math.round(d.similarity * 100)}%`
                            : `Similar · ${Math.round(d.similarity * 100)}%`}
                      </span>
                      {d.custodian && (
                        <div style={{ color: 'rgba(44,62,107,0.5)', fontSize: 9 }}>
                          Custodian: {d.custodian}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
            <FamilyList label="Family" items={family.family} onNavigate={onNavigate} />
            <FamilyList label="Thread" items={family.thread} onNavigate={onNavigate} />
          </div>
        </div>

        {/* CENTER — Document viewer */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          {showCenterTabs && (
            <div className="viewer-center-tabs">
              <button
                className={`viewer-center-tab ${centerTab === 'images' ? 'active' : ''}`}
                onClick={() => setCenterTab('images')}
              >
                Images
              </button>
              <button
                className={`viewer-center-tab ${centerTab === 'native' ? 'active' : ''}`}
                onClick={() => setCenterTab('native')}
              >
                Native
              </button>
            </div>
          )}
          {renderCenterPanel()}
        </div>

        {/* RIGHT SIDEBAR — Info (read-only) */}
        <div className="viewer-sidebar">
          <div className="tabs">
            <button className={`tab ${rightTab === 'text' ? 'active' : ''}`} onClick={() => setRightTab('text')}>
              Text
            </button>
            <button className={`tab ${rightTab === 'metadata' ? 'active' : ''}`} onClick={() => setRightTab('metadata')}>
              Metadata
            </button>
            <button className={`tab ${rightTab === 'summary' ? 'active' : ''}`} onClick={() => { setRightTab('summary'); if (!summary && !summaryLoading) handleSummarize(); }}>
              Summary
            </button>
          </div>

          <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
            {rightTab === 'text' && <TextPanel text={doc.text_content} searchQuery={searchQuery} />}
            {rightTab === 'metadata' && <MetadataPanel doc={doc} />}
            {rightTab === 'summary' && (
              <div style={{ padding: 'var(--space-4)', overflow: 'auto', flex: 1, fontSize: 'var(--text-sm)', lineHeight: 'var(--leading-relaxed)' }}>
                {summaryLoading ? (
                  <div className="loading-center"><span className="spinner spinner-sm" /> Generating summary...</div>
                ) : summary ? (
                  <p style={{ whiteSpace: 'pre-wrap', color: 'var(--color-neutral-700)' }}>{summary}</p>
                ) : (
                  <p style={{ color: 'var(--color-neutral-400)', fontStyle: 'italic' }}>No summary yet.</p>
                )}
              </div>
            )}
          </div>

          {/* AI Tools */}
          <div style={{ borderTop: '1px solid rgba(44,62,107,0.08)', padding: 'var(--space-2)', display: 'flex', gap: 'var(--space-1-5)', flexWrap: 'wrap' }}>
            <button className="btn btn-secondary btn-xs" onClick={handleSummarize} disabled={summaryLoading}>
              <span className="ai-indicator" style={{ padding: '0 3px', fontSize: 8 }}>AI</span>
              {summaryLoading ? 'Summarizing...' : 'Summarize'}
            </button>
            {onSearch && (
              <button className="btn btn-secondary btn-xs" onClick={handleFindSimilar} disabled={similarLoading}>
                <span className="ai-indicator" style={{ padding: '0 3px', fontSize: 8 }}>AI</span>
                {similarLoading ? 'Searching...' : 'Find Similar'}
              </button>
            )}
          </div>
        </div>
      </div>

      {popover && (
        <AnnotationPopover
          mode={popover.mode}
          position={popover.position}
          annotation={popover.annotation}
          selectedColor={popover.selectedColor}
          canDelete={true}
          onColorSelect={handleColorSelect}
          onSave={handleAnnotationSave}
          onUpdate={handleAnnotationUpdate}
          onDelete={handleAnnotationDelete}
          onCancel={() => setPopover(null)}
        />
      )}
    </div>
  );
}
