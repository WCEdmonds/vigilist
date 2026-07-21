import { useState } from 'react';
import type { DocumentDetail } from '../types';

interface Props {
  doc: DocumentDetail;
  onSummarize: () => void;
  onFindSimilar?: () => void;
  summarizing: boolean;
  findingSimilar: boolean;
  hasSummary: boolean;
}

export default function MetadataPanel({ doc, onSummarize, onFindSimilar, summarizing, findingSimilar, hasSummary }: Props) {
  const [open, setOpen] = useState(true);

  const fields: [string, string][] = [
    ['Bates Begin', doc.bates_begin],
    ['Bates End', doc.bates_end],
    ['Page Count', String(doc.page_count)],
    ['Production ID', String(doc.production_id)],
    ['Has Native', doc.native_path ? 'Yes' : 'No'],
    ...Object.entries(doc.metadata),
  ];

  return (
    <div style={{ borderTop: '1px solid var(--color-neutral-200)' }}>
      <button
        onClick={() => setOpen(!open)}
        className="panel-header"
        style={{ width: '100%', border: 'none', cursor: 'pointer' }}
      >
        <span style={{ fontSize: 11 }}>{open ? '▾' : '▸'}</span>
        Metadata
      </button>
      {open && (
        <div style={{ padding: 'var(--space-3)', maxHeight: 220, overflow: 'auto' }}>
          <table style={{ width: '100%', fontSize: 'var(--text-xs)' }}>
            <tbody>
              {fields.map(([key, val]) => (
                <tr key={key}>
                  <td style={{ padding: '3px 12px 3px 0', fontWeight: 600, whiteSpace: 'nowrap', verticalAlign: 'top', color: 'var(--color-neutral-500)' }}>{key}</td>
                  <td style={{ padding: '3px 0', wordBreak: 'break-all', color: 'var(--color-neutral-800)' }}>{val}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ borderTop: '1px solid var(--color-neutral-200)' }}>
        <div className="panel-header">
          <span className="brief-ai-mark">✦</span> AI tools
        </div>
        <div style={{ padding: 'var(--space-3)', display: 'flex', gap: 'var(--space-1-5)', flexWrap: 'wrap' }}>
          {!hasSummary && (
            <button className="btn btn-secondary btn-xs" onClick={onSummarize} disabled={summarizing}>
              <span className="brief-ai-mark">✦</span> {summarizing ? 'Summarizing…' : 'Summarize'}
            </button>
          )}
          {onFindSimilar && (
            <button className="btn btn-secondary btn-xs" onClick={onFindSimilar} disabled={findingSimilar}>
              <span className="brief-ai-mark">✦</span> {findingSimilar ? 'Finding…' : 'Find similar'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
