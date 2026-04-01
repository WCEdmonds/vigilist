import { forwardRef, useEffect, useState } from 'react';
import { getNativeUrl } from '../api/client';
import MediaPlayer, { type MediaPlayerHandle } from './MediaPlayer';

interface Props {
  docId: string;
  nativePath: string;
  onTimeUpdate?: (time: number) => void;
}

const MEDIA_EXTENSIONS = new Set(['mp4', 'mov', 'wav', 'mp3', 'avi', 'webm']);
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp']);
const IFRAME_EXTENSIONS = new Set(['pdf']);
const OFFICE_EXTENSIONS = new Set(['docx', 'doc', 'xlsx', 'xls', 'pptx', 'ppt']);

function getExtension(path: string): string {
  const dot = path.lastIndexOf('.');
  return dot >= 0 ? path.slice(dot + 1).toLowerCase() : '';
}

export { type MediaPlayerHandle };

const NativeViewer = forwardRef<MediaPlayerHandle, Props>(({ docId, nativePath, onTimeUpdate }, ref) => {
  const ext = getExtension(nativePath);
  const [nativeInfo, setNativeInfo] = useState<{ url: string; filename: string } | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    setNativeInfo(null);
    setError('');
    getNativeUrl(docId)
      .then(res => setNativeInfo({ url: res.url, filename: res.filename }))
      .catch(e => setError(e.message));
  }, [docId]);

  // Media files — use MediaPlayer with signed URL
  if (MEDIA_EXTENSIONS.has(ext)) {
    const mediaType = ext === 'wav' || ext === 'mp3' ? 'audio' : 'video';
    return <MediaPlayer ref={ref} docId={docId} mediaType={mediaType} onTimeUpdate={onTimeUpdate} />;
  }

  if (error) {
    return (
      <div className="viewer-main">
        <div className="empty-state" style={{ flex: 1 }}>
          <div style={{ color: 'var(--color-danger-600)' }}>Failed to load native file: {error}</div>
        </div>
      </div>
    );
  }

  if (!nativeInfo) {
    return (
      <div className="viewer-main">
        <div className="loading-center"><span className="spinner spinner-md" /> Loading native file...</div>
      </div>
    );
  }

  // PDFs — render in iframe
  if (IFRAME_EXTENSIONS.has(ext)) {
    return (
      <div className="viewer-main" style={{ flex: 1 }}>
        <iframe
          src={nativeInfo.url}
          title={nativeInfo.filename}
          style={{ width: '100%', height: '100%', border: 'none' }}
        />
      </div>
    );
  }

  // Images — render as img
  if (IMAGE_EXTENSIONS.has(ext)) {
    return (
      <div className="viewer-main">
        <div className="image-viewport" style={{ flexDirection: 'column', alignItems: 'center', padding: 32 }}>
          <img src={nativeInfo.url} alt={nativeInfo.filename} style={{ maxWidth: '100%', maxHeight: '80vh' }} />
        </div>
      </div>
    );
  }

  // Office docs — use Google Docs Viewer
  if (OFFICE_EXTENSIONS.has(ext)) {
    const googleViewerUrl = `https://docs.google.com/gview?url=${encodeURIComponent(nativeInfo.url)}&embedded=true`;
    return (
      <div className="viewer-main" style={{ flex: 1 }}>
        <iframe
          src={googleViewerUrl}
          title={nativeInfo.filename}
          style={{ width: '100%', height: '100%', border: 'none' }}
        />
      </div>
    );
  }

  // Fallback — download link
  return (
    <div className="viewer-main">
      <div className="empty-state" style={{ flex: 1 }}>
        <div style={{ fontSize: 'var(--text-lg)', fontFamily: 'var(--font-serif)' }}>Native File</div>
        <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)', marginTop: 'var(--space-1)' }}>
          This file type (.{ext}) cannot be previewed in the browser.
        </div>
        <a
          href={nativeInfo.url}
          className="btn btn-primary btn-sm"
          style={{ marginTop: 'var(--space-3)', textDecoration: 'none' }}
          download={nativeInfo.filename}
        >
          Download {nativeInfo.filename}
        </a>
      </div>
    </div>
  );
});

NativeViewer.displayName = 'NativeViewer';
export default NativeViewer;
