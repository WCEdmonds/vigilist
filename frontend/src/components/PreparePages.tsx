import { useEffect, useState } from 'react';
import { getDocument, preparePages } from '../api/client';
import type { DocumentDetail } from '../types';

interface Props {
  doc: DocumentDetail;
  /** Called with the refreshed document once its pages exist. */
  onReady: (doc: DocumentDetail) => void;
}

const POLL_MS = 3000;
const GIVE_UP_MS = 10 * 60 * 1000;

/**
 * Turn an imageless email, PDF or image into page images so it can be
 * redacted. Imaging runs on the server; this polls until it finishes.
 */
export default function PreparePages({ doc, onReady }: Props) {
  const [status, setStatus] = useState(doc.imaging_status ?? null);
  const [error, setError] = useState(doc.imaging_error ?? null);
  const [slow, setSlow] = useState(false);

  const running = status === 'queued' || status === 'running';

  useEffect(() => {
    if (!running) return;
    const started = Date.now();
    const timer = window.setInterval(async () => {
      if (Date.now() - started > GIVE_UP_MS) {
        window.clearInterval(timer);
        setSlow(true);
        return;
      }
      try {
        const fresh = await getDocument(doc.id);
        if (fresh.imaging_status === 'done' && (fresh.image_count ?? 0) > 0) {
          window.clearInterval(timer);
          onReady(fresh);
        } else if (fresh.imaging_status === 'failed') {
          window.clearInterval(timer);
          setStatus('failed');
          setError(fresh.imaging_error ?? 'Preparing pages failed');
        }
      } catch (e) {
        console.warn('Polling imaging status failed:', e);
      }
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, doc.id, onReady]);

  const start = async () => {
    setError(null);
    setSlow(false);
    try {
      const res = await preparePages(doc.id);
      setStatus(res.imaging_status);
      if (res.imaging_status === 'failed') setError('Could not start preparing pages');
    } catch (e) {
      setStatus('failed');
      setError(e instanceof Error ? e.message : 'Could not start preparing pages');
    }
  };

  return (
    <div className="prepare-pages-bar" role="status" style={{
      display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap',
      padding: 'var(--space-2) var(--space-3)', fontSize: 'var(--text-sm)',
      borderBottom: '1px solid rgba(44,62,107,0.1)', background: 'rgba(44,62,107,0.03)',
    }}>
      {running && !slow && (<><span className="spinner" /> Preparing pages for redaction…</>)}
      {running && slow && (
        <>
          <span>Taking longer than expected.</span>
          <button type="button" className="btn btn-ghost btn-xs" onClick={start}>Retry</button>
        </>
      )}
      {status === 'failed' && (
        <>
          <span style={{ color: 'var(--color-danger, #b42318)' }}>Couldn't prepare pages: {error}</span>
          <button type="button" className="btn btn-ghost btn-xs" onClick={start}>Retry</button>
        </>
      )}
      {!running && status !== 'failed' && (
        <>
          <span>To redact this {doc.file_type === 'email' ? 'email' : 'document'}, prepare it as pages first.</span>
          <button type="button" className="btn btn-secondary btn-xs" onClick={start}>Prepare pages for redaction</button>
        </>
      )}
    </div>
  );
}
