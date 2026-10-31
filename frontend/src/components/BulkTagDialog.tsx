import { useRef, useState } from 'react';
import { bulkTagApply, bulkTagDryRun } from '../api/client';
import type { BulkTagByQueryBody, BulkTagDryRun, BulkTagQuery, Tag } from '../types';

export type BulkTarget =
  | { kind: 'ids'; docIds: string[] }
  | { kind: 'query'; query: BulkTagQuery };

type Disposition = '' | 'withhold' | 'redact_in_part' | 'produce';
type ConflictAction = 'switch' | 'leave';

interface Props {
  target: BulkTarget;
  tags: Tag[];
  onClose: () => void;
  onApplied: (message: string) => void;
}

const tagRank = (t: Tag) => (t.category === 'responsiveness' ? 0 : t.is_privilege ? 1 : 2);
const n = (v: number) => v.toLocaleString();

/**
 * Apply one tag to a selection or to every document matching the current
 * search. Always counts first (dry run) so the reviewer sees exactly what
 * will change — attachments pulled in, and documents that already carry a
 * conflicting responsiveness tag, which they must choose to switch or leave.
 */
export default function BulkTagDialog({ target, tags, onClose, onApplied }: Props) {
  const [tagId, setTagId] = useState<number | null>(null);
  const [includeAttachments, setIncludeAttachments] = useState(true);
  const [conflictAction, setConflictAction] = useState<ConflictAction | null>(null);
  const [disposition, setDisposition] = useState<Disposition>('');
  const [description, setDescription] = useState('');
  const [overwriteExisting, setOverwriteExisting] = useState(false);
  const [preview, setPreview] = useState<BulkTagDryRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Only the latest count may land: changing the tag mid-request must not
  // let a stale response overwrite the new one. Touched only in handlers.
  const requestSeq = useRef(0);

  const sortedTags = [...tags].sort((a, b) => tagRank(a) - tagRank(b) || a.name.localeCompare(b.name));
  const tag = tags.find(t => t.id === tagId) ?? null;

  const baseBody = (forTagId: number, withAttachments: boolean): Omit<BulkTagByQueryBody, 'dry_run'> => ({
    ...(target.kind === 'ids' ? { doc_ids: target.docIds } : { query: target.query }),
    tag_id: forTagId,
    include_attachments: withAttachments,
  });

  const runPreview = async (forTagId: number | null, withAttachments: boolean) => {
    const seq = ++requestSeq.current;
    setPreview(null);
    setError(null);
    setConflictAction(null);
    if (forTagId === null) return;
    try {
      const res = await bulkTagDryRun(baseBody(forTagId, withAttachments));
      if (seq === requestSeq.current) setPreview(res);
    } catch (e) {
      if (seq === requestSeq.current) setError(e instanceof Error ? e.message : 'Could not count documents');
    }
  };

  const apply = async () => {
    if (!tag || !preview || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await bulkTagApply({
        ...baseBody(tag.id, includeAttachments),
        conflict_action: preview.conflicts > 0 ? conflictAction : null,
        disposition: tag.is_privilege && disposition ? disposition : null,
        description: tag.is_privilege && description.trim() ? description.trim() : null,
        overwrite_existing: overwriteExisting,
      });
      const parts = [`Tagged ${n(res.tagged)} document${res.tagged === 1 ? '' : 's'} "${tag.name}"`];
      if (res.switched) parts.push(`${n(res.switched)} switched`);
      if (res.left) parts.push(`${n(res.left)} left as they were`);
      if (res.already_tagged) parts.push(`${n(res.already_tagged)} already tagged`);
      if (res.skipped_existing_overrides) parts.push(`${n(res.skipped_existing_overrides)} kept their existing privilege log entry`);
      onApplied(parts.join(' · '));
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'unknown error';
      if (msg === 'conflicts_unresolved') {
        // Conflicts appeared since the count — recount and ask again.
        await runPreview(tag.id, includeAttachments);
      } else {
        setError(`Could not apply tag: ${msg}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const needsChoice = !!preview && preview.conflicts > 0 && conflictAction === null;
  const conflictNames = preview?.conflict_tags.map(c => c.name).join(' / ') ?? '';
  const applyCount = preview
    ? preview.total - (conflictAction === 'leave' ? preview.conflicts : 0)
    : 0;

  return (
    <div className="modal-overlay" onClick={() => { if (!busy) onClose(); }}>
      <div className="modal-panel" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="bulk-tag-title">
        <div className="modal-header">
          <h2 className="modal-title" id="bulk-tag-title">
            {target.kind === 'query' ? 'Tag all matching documents' : 'Tag selected documents'}
          </h2>
          <button className="modal-close-btn" aria-label="Close" onClick={onClose} disabled={busy}>&times;</button>
        </div>

        <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: 'var(--text-sm)', fontWeight: 600 }}>Tag</span>
            <select
              className="input"
              value={tagId ?? ''}
              onChange={e => {
                const next = e.target.value ? Number(e.target.value) : null;
                setTagId(next);
                runPreview(next, includeAttachments);
              }}
              disabled={busy}
            >
              <option value="">Choose a tag…</option>
              {sortedTags.map(t => (
                <option key={t.id} value={t.id}>{t.name}{t.is_privilege ? ' (privilege)' : ''}</option>
              ))}
            </select>
          </label>

          <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
            <input
              type="checkbox"
              checked={includeAttachments}
              onChange={e => {
                setIncludeAttachments(e.target.checked);
                runPreview(tagId, e.target.checked);
              }}
              disabled={busy}
            />
            Include attachments
          </label>

          {tagId !== null && !preview && !error && (
            <div style={{ fontSize: 'var(--text-sm)', color: 'rgba(44,62,107,0.6)' }}>Counting documents…</div>
          )}

          {preview && (
            <div style={{ fontSize: 'var(--text-sm)' }}>
              Apply to <strong>{n(preview.total)}</strong> document{preview.total === 1 ? '' : 's'}
              {' '}({n(preview.matched)} matched
              {includeAttachments ? ` + ${n(preview.attachments_added)} attachments` : ''}
              {preview.already_tagged ? `; ${n(preview.already_tagged)} already tagged` : ''})
            </div>
          )}

          {preview && preview.conflicts > 0 && tag && (
            <fieldset style={{ border: '1px solid var(--color-neutral-200)', borderRadius: 'var(--radius-sm)', padding: 'var(--space-2) var(--space-3)' }}>
              <legend style={{ fontSize: 'var(--text-sm)', padding: '0 4px' }}>
                <strong>{n(preview.conflicts)}</strong> of these {preview.conflicts === 1 ? 'is' : 'are'} currently tagged <em>{conflictNames}</em>.
              </legend>
              <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
                <input type="radio" name="conflict" checked={conflictAction === 'switch'}
                  onChange={() => setConflictAction('switch')} disabled={busy} />
                Switch them to {tag.name}
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
                <input type="radio" name="conflict" checked={conflictAction === 'leave'}
                  onChange={() => setConflictAction('leave')} disabled={busy} />
                Leave them as they are
              </label>
            </fieldset>
          )}

          {tag?.is_privilege && (
            <details>
              <summary style={{ fontSize: 'var(--text-sm)', cursor: 'pointer' }}>Privilege log options (managers only)</summary>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)', marginTop: 'var(--space-2)' }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--text-sm)' }}>
                  Disposition
                  <select className="input input-sm" value={disposition}
                    onChange={e => setDisposition(e.target.value as Disposition)} disabled={busy}>
                    <option value="">No override (withhold, from the tag)</option>
                    <option value="withhold">Withhold</option>
                    <option value="redact_in_part">Redact in part</option>
                    <option value="produce">Produce</option>
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--text-sm)' }}>
                  Log description (replaces the generated one; documents that already have a hand-written entry keep it unless you choose to overwrite)
                  <textarea className="input" rows={3} value={description}
                    onChange={e => setDescription(e.target.value)} disabled={busy}
                    placeholder="e.g. Communication with outside counsel re: litigation strategy" />
                </label>
                {preview && preview.existing_overrides > 0 && (disposition || description.trim()) && (
                  <label style={{ display: 'flex', alignItems: 'flex-start', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
                    <input type="checkbox" checked={overwriteExisting}
                      onChange={e => setOverwriteExisting(e.target.checked)} disabled={busy} />
                    <span>
                      <strong>{n(preview.existing_overrides)}</strong> of these already have a privilege log entry.
                      Overwrite them too? (Their current text is saved in the audit log.)
                    </span>
                  </label>
                )}
              </div>
            </details>
          )}

          {error && <div role="alert" style={{ color: 'var(--color-danger, #b42318)', fontSize: 'var(--text-sm)' }}>{error}</div>}
        </div>

        <div className="modal-footer" style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--space-2)' }}>
          <button className="btn btn-secondary btn-sm" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-primary btn-sm" onClick={apply} disabled={!preview || busy || needsChoice || applyCount === 0}>
            {busy ? 'Applying…' : preview ? `Apply to ${n(applyCount)} document${applyCount === 1 ? '' : 's'}` : 'Apply'}
          </button>
        </div>
      </div>
    </div>
  );
}
