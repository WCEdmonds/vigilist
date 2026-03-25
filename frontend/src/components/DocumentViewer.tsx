import { useCallback, useEffect, useState } from 'react';
import { findSimilar, getDocument, getDocumentNav, nativeUrl, summarizeDocument } from '../api/client';
import type { DocumentDetail, DocumentTagEntry } from '../types';
import DocumentNav from './DocumentNav';
import ImagePanel from './ImagePanel';
import MediaPlayer from './MediaPlayer';
import MetadataPanel from './MetadataPanel';
import NotesPanel from './NotesPanel';
import TagBar from './TagBar';
import TextPanel from './TextPanel';

const STREAMABLE_EXTENSIONS = new Set(['.mp4', '.mov', '.wav']);

function getStreamableInfo(nativePath: string | null): { streamable: boolean; mediaType: 'video' | 'audio' } | null {
  if (!nativePath) return null;
  const ext = nativePath.slice(nativePath.lastIndexOf('.')).toLowerCase();
  if (!STREAMABLE_EXTENSIONS.has(ext)) return null;
  return { streamable: true, mediaType: ext === '.wav' ? 'audio' : 'video' };
}

interface Props {
  docId: string;
  onNavigate: (id: string) => void;
  onBack: () => void;
  searchQuery?: string;
  onSearch?: (query: string) => void;
}

type RightTab = 'text' | 'metadata' | 'summary';
type CenterTab = 'images' | 'native';

export default function DocumentViewer({ docId, onNavigate, onBack, searchQuery, onSearch }: Props) {
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState('');
  const [rightTab, setRightTab] = useState<RightTab>('text');
  const [centerTab, setCenterTab] = useState<CenterTab>('images');
  const [nextId, setNextId] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [similarLoading, setSimilarLoading] = useState(false);

  useEffect(() => {
    setError('');
    setSummary(null);
    setCenterTab('images');
    getDocument(docId).then(d => {
      setDoc(d);
      if (d.summary) setSummary(d.summary);
    }).catch(e => setError(e.message));
    getDocumentNav(docId).then(nav => setNextId(nav.next_id));
  }, [docId]);

  const handleTagsChanged = useCallback((tags: DocumentTagEntry[]) => {
    if (doc) setDoc({ ...doc, tags });
  }, [doc]);

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
    } catch (e: any) {
      setSummary(`Error: ${e.message}`);
    } finally {
      setSummaryLoading(false);
    }
  };

  const handleFindSimilar = async () => {
    if (!onSearch) return;
    setSimilarLoading(true);
    try {
      const res = await findSimilar(docId);
      onSearch(res.search_terms);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSimilarLoading(false);
    }
  };

  if (error) return <div style={{ padding: 32, color: 'var(--color-danger-600)' }}>Error: {error}</div>;
  if (!doc) return <div className="loading-center"><span className="spinner spinner-md" /> Loading document...</div>;

  const hasNative = !!doc.native_path;
  const hasImages = doc.page_count > 0;
  const streamInfo = getStreamableInfo(doc.native_path);
  const showCenterTabs = streamInfo && hasImages;

  const renderCenterPanel = () => {
    // Streamable native with tab set to native, or streamable-only (no images)
    if (streamInfo && (centerTab === 'native' || !hasImages)) {
      return <MediaPlayer docId={doc.id} mediaType={streamInfo.mediaType} />;
    }

    // Has images (default)
    if (hasImages) {
      return <ImagePanel docId={doc.id} pageCount={doc.page_count} />;
    }

    // Non-streamable native only
    if (hasNative) {
      return (
        <div className="viewer-main">
          <div className="empty-state" style={{ flex: 1 }}>
            <div style={{ fontSize: 'var(--text-lg)', fontFamily: 'var(--font-serif)' }}>Native File Only</div>
            <div>This document has no page images.</div>
            <a href={nativeUrl(doc.id)} className="btn btn-primary btn-sm" style={{ marginTop: 'var(--space-2)', textDecoration: 'none' }} download>
              Download {doc.native_path?.split(/[/\\]/).pop() || 'File'}
            </a>
          </div>
        </div>
      );
    }

    return (
      <div className="viewer-main">
        <div className="empty-state" style={{ flex: 1 }}>
          <div>No viewable content for this document.</div>
        </div>
      </div>
    );
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      {/* Header */}
      <div className="app-header">
        <button className="btn-header" onClick={onBack}>← Back</button>
        <span className="logo">Vigilist</span>
      </div>

      {/* Nav bar */}
      <DocumentNav doc={doc} onNavigate={onNavigate} searchQuery={searchQuery} />

      {/* Main content — three-column layout */}
      <div className="viewer-layout">
        {/* LEFT SIDEBAR — Actions */}
        <div className="viewer-left-sidebar">
          {/* Tags section */}
          <div className="sidebar-section">
            <div className="sidebar-section-title">Tags</div>
            <TagBar
              docId={doc.id}
              tags={doc.tags}
              onTagsChanged={handleTagsChanged}
              onAutoAdvance={handleAutoAdvance}
            />
          </div>

          {/* Notes section */}
          <div className="sidebar-section sidebar-section-grow">
            <div className="sidebar-section-title">Notes</div>
            <NotesPanel docId={doc.id} />
          </div>

          {/* AI Actions */}
          <div className="sidebar-section">
            <div className="sidebar-section-title">AI Tools</div>
            <div style={{ padding: 'var(--space-2)', display: 'flex', flexDirection: 'column', gap: 'var(--space-1-5)' }}>
              <button className="btn btn-secondary btn-sm" style={{ width: '100%', justifyContent: 'flex-start' }} onClick={handleSummarize} disabled={summaryLoading}>
                <span className="ai-indicator" style={{ padding: '0 4px', fontSize: 9 }}>AI</span>
                {summaryLoading ? 'Generating...' : 'Summarize'}
              </button>
              {onSearch && (
                <button className="btn btn-secondary btn-sm" style={{ width: '100%', justifyContent: 'flex-start' }} onClick={handleFindSimilar} disabled={similarLoading}>
                  <span className="ai-indicator" style={{ padding: '0 4px', fontSize: 9 }}>AI</span>
                  {similarLoading ? 'Searching...' : 'Find Similar'}
                </button>
              )}
              {hasNative && (
                <a href={nativeUrl(doc.id)} className="btn btn-secondary btn-sm" style={{ width: '100%', justifyContent: 'flex-start', textDecoration: 'none' }} download>
                  Download Native
                </a>
              )}
            </div>
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
                {streamInfo.mediaType === 'audio' ? 'Audio' : 'Video'}
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
                  <p style={{ color: 'var(--color-neutral-400)', fontStyle: 'italic' }}>No summary yet. Click "Summarize" in the left panel to generate one.</p>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
