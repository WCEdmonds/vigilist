import { useEffect, useRef, useState } from 'react';
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
  mode: 'color-picker' | 'create' | 'view';
  position: { top: number; left: number };
  annotation?: Annotation;
  selectedColor?: string;
  canDelete?: boolean;
  onColorSelect: (color: string) => void;
  onSave: (content: string) => void;
  onUpdate: (data: { content?: string; color?: string }) => void;
  onDelete: () => void;
  onCancel: () => void;
}

export default function AnnotationPopover({
  mode,
  position,
  annotation,
  selectedColor,
  canDelete,
  onColorSelect,
  onSave,
  onUpdate,
  onDelete,
  onCancel,
}: Props) {
  const [content, setContent] = useState(annotation?.content ?? '');
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState(annotation?.content ?? '');
  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef(content);
  contentRef.current = content;

  // Sync content when annotation changes
  useEffect(() => {
    setContent(annotation?.content ?? '');
    setEditContent(annotation?.content ?? '');
    setEditing(false);
  }, [annotation]);

  // Click-outside handler
  useEffect(() => {
    const handleMouseDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        if (mode === 'create') {
          onSave(contentRef.current);
        } else {
          onCancel();
        }
      }
    };
    document.addEventListener('mousedown', handleMouseDown);
    return () => document.removeEventListener('mousedown', handleMouseDown);
  }, [mode, onSave, onCancel]);

  // Escape key handler
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onCancel();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onCancel]);

  const containerStyle: React.CSSProperties = {
    position: 'fixed',
    top: position.top,
    left: position.left,
    zIndex: 1000,
    background: 'var(--color-surface, #fff)',
    border: '1px solid var(--color-border, #e2e8f0)',
    borderRadius: 'var(--radius-md, 8px)',
    boxShadow: '0 4px 16px rgba(0,0,0,0.15)',
    minWidth: 200,
    maxWidth: 280,
    padding: 'var(--space-3, 12px)',
  };

  if (mode === 'color-picker') {
    return (
      <div ref={containerRef} style={containerStyle}>
        <div style={{ fontSize: 'var(--text-xs, 11px)', color: 'var(--color-text-muted, #718096)', marginBottom: 8, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          Pin color
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {Object.entries(PIN_COLORS).map(([name, hex]) => (
            <button
              key={name}
              onClick={() => onColorSelect(name)}
              title={name}
              style={{
                width: 28,
                height: 28,
                borderRadius: '50%',
                background: hex,
                border: selectedColor === name ? '3px solid var(--color-text, #1a202c)' : '2px solid white',
                outline: selectedColor === name ? '2px solid var(--color-text, #1a202c)' : 'none',
                cursor: 'pointer',
                padding: 0,
                boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
              }}
            />
          ))}
          <button
            onClick={onCancel}
            style={{
              marginLeft: 'auto',
              fontSize: 'var(--text-xs, 11px)',
              color: 'var(--color-text-muted, #718096)',
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              padding: '2px 6px',
            }}
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  if (mode === 'create') {
    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && e.ctrlKey) {
        onSave(content);
      }
    };

    return (
      <div ref={containerRef} style={containerStyle}>
        <textarea
          autoFocus
          value={content}
          onChange={e => setContent(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Add a note... (optional)"
          rows={3}
          style={{
            width: '100%',
            resize: 'vertical',
            fontSize: 'var(--text-sm, 13px)',
            border: '1px solid var(--color-border, #e2e8f0)',
            borderRadius: 'var(--radius-sm, 4px)',
            padding: '6px 8px',
            fontFamily: 'inherit',
            outline: 'none',
            boxSizing: 'border-box',
            background: 'var(--color-bg, #f7fafc)',
            color: 'var(--color-text, #1a202c)',
          }}
        />
        <div style={{ display: 'flex', gap: 6, marginTop: 8, justifyContent: 'flex-end' }}>
          <button
            onClick={onCancel}
            style={{
              fontSize: 'var(--text-sm, 13px)',
              padding: '4px 10px',
              background: 'none',
              border: '1px solid var(--color-border, #e2e8f0)',
              borderRadius: 'var(--radius-sm, 4px)',
              cursor: 'pointer',
              color: 'var(--color-text-muted, #718096)',
            }}
          >
            Cancel
          </button>
          <button
            onClick={() => onSave(content)}
            style={{
              fontSize: 'var(--text-sm, 13px)',
              padding: '4px 10px',
              background: 'var(--color-primary, #4299e1)',
              border: 'none',
              borderRadius: 'var(--radius-sm, 4px)',
              cursor: 'pointer',
              color: 'white',
              fontWeight: 600,
            }}
          >
            Save
          </button>
        </div>
        <div style={{ marginTop: 4, fontSize: 'var(--text-xs, 11px)', color: 'var(--color-text-muted, #718096)' }}>
          Ctrl+Enter to save
        </div>
      </div>
    );
  }

  // view mode
  if (!annotation) return null;

  const displayName = annotation.created_by_display_name || annotation.created_by_email;

  if (editing) {
    const handleEditKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && e.ctrlKey) {
        onUpdate({ content: editContent });
        setEditing(false);
      }
    };

    return (
      <div ref={containerRef} style={containerStyle}>
        <textarea
          autoFocus
          value={editContent}
          onChange={e => setEditContent(e.target.value)}
          onKeyDown={handleEditKeyDown}
          rows={3}
          style={{
            width: '100%',
            resize: 'vertical',
            fontSize: 'var(--text-sm, 13px)',
            border: '1px solid var(--color-border, #e2e8f0)',
            borderRadius: 'var(--radius-sm, 4px)',
            padding: '6px 8px',
            fontFamily: 'inherit',
            outline: 'none',
            boxSizing: 'border-box',
            background: 'var(--color-bg, #f7fafc)',
            color: 'var(--color-text, #1a202c)',
          }}
        />
        <div style={{ display: 'flex', gap: 6, marginTop: 8, justifyContent: 'flex-end' }}>
          <button
            onClick={() => setEditing(false)}
            style={{
              fontSize: 'var(--text-sm, 13px)',
              padding: '4px 10px',
              background: 'none',
              border: '1px solid var(--color-border, #e2e8f0)',
              borderRadius: 'var(--radius-sm, 4px)',
              cursor: 'pointer',
              color: 'var(--color-text-muted, #718096)',
            }}
          >
            Cancel
          </button>
          <button
            onClick={() => { onUpdate({ content: editContent }); setEditing(false); }}
            style={{
              fontSize: 'var(--text-sm, 13px)',
              padding: '4px 10px',
              background: 'var(--color-primary, #4299e1)',
              border: 'none',
              borderRadius: 'var(--radius-sm, 4px)',
              cursor: 'pointer',
              color: 'white',
              fontWeight: 600,
            }}
          >
            Save
          </button>
        </div>
      </div>
    );
  }

  return (
    <div ref={containerRef} style={containerStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
        <span
          style={{
            width: 12,
            height: 12,
            borderRadius: '50%',
            background: PIN_COLORS[annotation.color] || PIN_COLORS.blue,
            flexShrink: 0,
          }}
        />
        <span style={{ fontSize: 'var(--text-xs, 11px)', color: 'var(--color-text-muted, #718096)', fontWeight: 600 }}>
          Page {annotation.page_num}
        </span>
      </div>

      {annotation.content ? (
        <div style={{ fontSize: 'var(--text-sm, 13px)', color: 'var(--color-text, #1a202c)', marginBottom: 8, lineHeight: 1.5 }}>
          {annotation.content}
        </div>
      ) : (
        <div style={{ fontSize: 'var(--text-sm, 13px)', color: 'var(--color-text-muted, #718096)', fontStyle: 'italic', marginBottom: 8 }}>
          No note
        </div>
      )}

      <div style={{ fontSize: 'var(--text-xs, 11px)', color: 'var(--color-text-muted, #718096)', marginBottom: 10 }}>
        {displayName} &middot; {timeAgo(annotation.created_at)}
      </div>

      <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
        {canDelete && (
          <button
            onClick={onDelete}
            style={{
              fontSize: 'var(--text-sm, 13px)',
              padding: '4px 10px',
              background: 'none',
              border: '1px solid var(--color-danger, #e53e3e)',
              borderRadius: 'var(--radius-sm, 4px)',
              cursor: 'pointer',
              color: 'var(--color-danger, #e53e3e)',
            }}
          >
            Delete
          </button>
        )}
        <button
          onClick={() => { setEditContent(annotation.content); setEditing(true); }}
          style={{
            fontSize: 'var(--text-sm, 13px)',
            padding: '4px 10px',
            background: 'var(--color-primary, #4299e1)',
            border: 'none',
            borderRadius: 'var(--radius-sm, 4px)',
            cursor: 'pointer',
            color: 'white',
            fontWeight: 600,
          }}
        >
          Edit
        </button>
      </div>
    </div>
  );
}
