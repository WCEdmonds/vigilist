import { useCallback, useEffect, useRef, useState } from 'react';
import { applyTags, getTags, removeTag } from '../api/client';
import type { DocumentTagEntry, Tag } from '../types';

interface Props {
  docId: string;
  tags: DocumentTagEntry[];
  onTagsChanged: (tags: DocumentTagEntry[]) => void;
  onAutoAdvance?: () => void;
}

const COLOR_MAP: Record<string, string> = {
  green: 'badge-green', red: 'badge-red', yellow: 'badge-yellow',
  purple: 'badge-purple', gray: 'badge-gray', blue: 'badge-blue',
};

export default function TagBar({ docId, tags, onTagsChanged, onAutoAdvance }: Props) {
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [showDropdown, setShowDropdown] = useState(false);
  const [flashClass, setFlashClass] = useState('');
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => { getTags().then(setAllTags); }, []);

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const appliedTagIds = new Set(tags.map(t => t.tag.id));

  const toggleTag = useCallback(async (tagId: number) => {
    if (appliedTagIds.has(tagId)) {
      await removeTag(docId, tagId);
      onTagsChanged(tags.filter(t => t.tag.id !== tagId));
      setFlashClass('flash-remove');
    } else {
      const updated = await applyTags(docId, [tagId]);
      onTagsChanged(updated);
      setFlashClass('flash-success');
      setTimeout(() => onAutoAdvance?.(), 300);
    }
    setTimeout(() => setFlashClass(''), 600);
  }, [docId, tags, appliedTagIds, onTagsChanged, onAutoAdvance]);

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      const tag = allTags.find(t => t.keyboard_shortcut === e.key);
      if (tag) {
        e.preventDefault();
        toggleTag(tag.id);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [allTags, toggleTag]);

  // Group tags by category
  const categories = [...new Set(allTags.map(t => t.category))];

  return (
    <div className={`tag-bar ${flashClass}`}>
      <span className="tag-bar-label">Tags</span>
      {tags.map(dt => (
        <span key={dt.id} className={`badge ${COLOR_MAP[dt.tag.color] || 'badge-gray'}`}>
          {dt.tag.name}
          <span className="badge-remove" onClick={() => toggleTag(dt.tag.id)}>×</span>
        </span>
      ))}

      <div style={{ position: 'relative' }} ref={dropdownRef}>
        <button className="btn btn-ghost btn-xs" onClick={() => setShowDropdown(!showDropdown)}>
          + Tag
        </button>
        {showDropdown && (
          <div className="dropdown" style={{ top: '100%', left: 0, marginTop: 4 }}>
            {categories.map(cat => (
              <div key={cat}>
                <div className="dropdown-header">{cat}</div>
                {allTags.filter(t => t.category === cat).map(tag => (
                  <div
                    key={tag.id}
                    className="dropdown-item"
                    onClick={() => { toggleTag(tag.id); setShowDropdown(false); }}
                  >
                    <span className={`badge ${COLOR_MAP[tag.color] || 'badge-gray'}`} style={{ fontSize: 10 }}>
                      {appliedTagIds.has(tag.id) ? '✓ ' : ''}{tag.name}
                    </span>
                    {tag.keyboard_shortcut && (
                      <span className="kbd" style={{ marginLeft: 'auto' }}>{tag.keyboard_shortcut}</span>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
