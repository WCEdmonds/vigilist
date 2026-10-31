interface Props {
  parentId?: string | null;
  parentTitle?: string | null;
  parentBates?: string | null;
  parentIsEmail?: boolean | null;
  /** Opens the parent document; the badge is plain text without it. */
  onOpenParent?: (id: string) => void;
}

/**
 * Marks a document as an attachment so it doesn't read as a standalone email
 * in lists and the viewer — "📎 Attachment to <subject>" (or "From <archive>"
 * for files that came out of a zip), linking to the parent.
 */
export default function AttachmentBadge({ parentId, parentTitle, parentBates, parentIsEmail, onOpenParent }: Props) {
  if (!parentId) return null;
  const label = `${parentIsEmail === false ? '📦 From' : '📎 Attachment to'} ${parentTitle || parentBates}`;
  const tip = `Parent: ${parentBates}${parentTitle && parentTitle !== parentBates ? ` — ${parentTitle}` : ''}`;
  const style = {
    display: 'inline-flex', alignItems: 'center', maxWidth: '100%', fontSize: 'var(--text-xs)',
    color: 'var(--color-neutral-500)', background: 'none', border: 'none', padding: 0,
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' as const,
  };
  if (!onOpenParent) return <span style={style} title={tip}>{label}</span>;
  return (
    <button
      type="button"
      className="attachment-badge"
      style={{ ...style, cursor: 'pointer', textDecoration: 'underline dotted' }}
      title={`${tip} (open)`}
      onClick={e => { e.stopPropagation(); onOpenParent(parentId); }}
    >
      {label}
    </button>
  );
}
