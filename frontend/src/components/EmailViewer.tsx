import { useEffect, useState } from 'react';
import { getDocumentEmail } from '../api/client';
import type { EmailView } from '../types';

interface Props {
  docId: string;
  onOpenAttachment: (id: string) => void;
}

// The email HTML is untrusted: it renders in a sandboxed iframe with no
// scripts and no same-origin access, and a CSP that blocks every network
// fetch (tracking pixels, remote fonts) unless the reviewer opts in to
// remote images. Inline cid: images were rewritten to data: URIs at ingest.
const cspFor = (allowRemoteImages: boolean) =>
  `default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:${allowRemoteImages ? ' https: http:' : ''}`;

const buildSrcDoc = (html: string, allowRemoteImages: boolean) =>
  `<!doctype html><html><head><meta charset="utf-8">` +
  `<meta http-equiv="Content-Security-Policy" content="${cspFor(allowRemoteImages)}">` +
  `<base target="_blank">` +
  `<style>body{margin:16px;font-family:system-ui,sans-serif;font-size:14px;color:#1a1a1a;background:#fff;word-wrap:break-word}img{max-width:100%;height:auto}</style>` +
  `</head><body>${html}</body></html>`;

const HEADER_ROWS: [keyof EmailView, string][] = [
  ['email_from', 'From'],
  ['email_to', 'To'],
  ['email_cc', 'Cc'],
  ['email_bcc', 'Bcc'],
];

export default function EmailViewer({ docId, onOpenAttachment }: Props) {
  const [email, setEmail] = useState<EmailView | null>(null);
  const [error, setError] = useState('');
  const [remoteImages, setRemoteImages] = useState(false);
  const [showText, setShowText] = useState(false);

  useEffect(() => {
    // Mounted with key={docId}, so state starts fresh for each document.
    let cancelled = false;
    getDocumentEmail(docId)
      .then(e => { if (!cancelled) setEmail(e); })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load email'); });
    return () => { cancelled = true; };
  }, [docId]);

  if (error) return <div className="viewer-main" style={{ padding: 32, color: 'var(--color-danger-600)' }}>Error: {error}</div>;
  if (!email) return (
    <div className="viewer-main">
      <div className="empty-state" style={{ flex: 1 }}><span className="spinner" /></div>
    </div>
  );

  const hasHtml = !!email.body_html;
  const date = email.date_sent ? new Date(email.date_sent).toLocaleString() : null;

  return (
    <div className="viewer-main" style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, background: 'var(--color-neutral-50)' }}>
      <div style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--color-neutral-200)', background: 'var(--color-white, #fff)' }}>
        <div style={{ fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)', marginBottom: 'var(--space-2)', wordBreak: 'break-word' }}>
          {email.email_subject || '(no subject)'}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', columnGap: 'var(--space-3)', rowGap: 2, fontSize: 'var(--text-sm)' }}>
          {HEADER_ROWS.filter(([k]) => email[k]).map(([k, label]) => (
            <div key={k} style={{ display: 'contents' }}>
              <span style={{ color: 'var(--color-neutral-500)' }}>{label}</span>
              <span style={{ wordBreak: 'break-word' }}>{email[k] as string}</span>
            </div>
          ))}
          {date && (
            <>
              <span style={{ color: 'var(--color-neutral-500)' }}>Date</span>
              <span>{date}</span>
            </>
          )}
        </div>
        {email.attachments.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', marginTop: 'var(--space-3)' }}>
            {email.attachments.map(a => (
              <button
                key={a.id}
                className="btn btn-secondary btn-sm"
                onClick={() => onOpenAttachment(a.id)}
                title={a.bates_begin}
                style={{ maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
              >
                📎 {a.file_name || a.bates_begin}
              </button>
            ))}
          </div>
        )}
        {hasHtml && (
          <div style={{ display: 'flex', gap: 'var(--space-3)', marginTop: 'var(--space-3)', fontSize: 'var(--text-xs)' }}>
            <button className="btn btn-ghost btn-sm" onClick={() => setShowText(s => !s)}>
              {showText ? 'Show formatted' : 'Show plain text'}
            </button>
            {!showText && (
              <button className="btn btn-ghost btn-sm" onClick={() => setRemoteImages(r => !r)}>
                {remoteImages ? 'Block remote images' : 'Load remote images'}
              </button>
            )}
          </div>
        )}
      </div>
      {hasHtml && !showText ? (
        <iframe
          key={remoteImages ? 'remote' : 'local'}
          title={email.email_subject || 'Email'}
          sandbox="allow-popups allow-popups-to-escape-sandbox"
          srcDoc={buildSrcDoc(email.body_html!, remoteImages)}
          style={{ flex: 1, width: '100%', border: 'none', background: '#fff' }}
        />
      ) : (
        <pre style={{ flex: 1, margin: 0, padding: 'var(--space-4)', overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'inherit', fontSize: 'var(--text-sm)', background: 'var(--color-white, #fff)' }}>
          {email.body_text || '(empty message)'}
        </pre>
      )}
    </div>
  );
}
