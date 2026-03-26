import type { Annotation } from '../types';

const PIN_COLORS: Record<string, string> = {
  red: '#e53e3e',
  yellow: '#ecc94b',
  green: '#48bb78',
  blue: '#4299e1',
};

function timeAgo(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 60) return 'just now';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.floor(diffHour / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  const diffMonth = Math.floor(diffDay / 30);
  if (diffMonth < 12) return `${diffMonth}mo ago`;
  return `${Math.floor(diffMonth / 12)}y ago`;
}

interface Props {
  annotations: Annotation[];
  rotation: number;
  pageCount: number;
  onSelect: (annotation: Annotation) => void;
}

export default function AnnotationSidebar({ annotations, rotation, pageCount, onSelect }: Props) {
  if (pageCount === 0) {
    return (
      <div style={{ padding: 'var(--space-4, 16px)', color: 'var(--color-text-muted, #718096)', fontSize: 'var(--text-sm, 13px)' }}>
        No pages available for annotation.
      </div>
    );
  }

  if (rotation !== 0) {
    return (
      <div style={{
        margin: 'var(--space-3, 12px)',
        padding: 'var(--space-3, 12px)',
        background: 'var(--color-warning-bg, #fffbeb)',
        border: '1px solid var(--color-warning-border, #f6e05e)',
        borderRadius: 'var(--radius-sm, 4px)',
        color: 'var(--color-warning-text, #744210)',
        fontSize: 'var(--text-sm, 13px)',
      }}>
        Rotate to 0° to place or view pins on the page.
      </div>
    );
  }

  if (annotations.length === 0) {
    return (
      <div style={{ padding: 'var(--space-4, 16px)', color: 'var(--color-text-muted, #718096)', fontSize: 'var(--text-sm, 13px)' }}>
        No annotations yet. Click on a page image to add one.
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {annotations.map((ann) => {
        const displayName = ann.created_by_display_name || ann.created_by_email;
        const pinColor = PIN_COLORS[ann.color] || PIN_COLORS.blue;

        return (
          <button
            key={ann.id}
            onClick={() => onSelect(ann)}
            style={{
              display: 'block',
              width: '100%',
              textAlign: 'left',
              background: 'none',
              border: 'none',
              borderLeft: `4px solid ${pinColor}`,
              padding: 'var(--space-3, 12px) var(--space-3, 12px) var(--space-3, 12px) calc(var(--space-3, 12px) - 4px)',
              cursor: 'pointer',
              borderRadius: 0,
              transition: 'background 0.1s',
            }}
            onMouseEnter={e => { (e.currentTarget as HTMLButtonElement).style.background = 'var(--color-hover, rgba(30,24,16,0.06))'; }}
            onMouseLeave={e => { (e.currentTarget as HTMLButtonElement).style.background = 'none'; }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
              <span
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: '50%',
                  background: pinColor,
                  flexShrink: 0,
                }}
              />
              <span style={{ fontSize: 'var(--text-xs, 11px)', color: 'var(--color-text-muted, #718096)', fontWeight: 600 }}>
                Page {ann.page_num}
              </span>
            </div>

            {ann.content ? (
              <div
                style={{
                  fontSize: 'var(--text-sm, 13px)',
                  color: 'var(--color-text, #1a202c)',
                  lineHeight: 1.4,
                  marginBottom: 4,
                  display: '-webkit-box',
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: 'vertical',
                  overflow: 'hidden',
                }}
              >
                {ann.content}
              </div>
            ) : (
              <div
                style={{
                  fontSize: 'var(--text-sm, 13px)',
                  color: 'var(--color-text-muted, #718096)',
                  fontStyle: 'italic',
                  marginBottom: 4,
                }}
              >
                No note
              </div>
            )}

            <div style={{ fontSize: 'var(--text-xs, 11px)', color: 'var(--color-text-muted, #718096)' }}>
              {displayName} &middot; {timeAgo(ann.created_at)}
            </div>
          </button>
        );
      })}
    </div>
  );
}
