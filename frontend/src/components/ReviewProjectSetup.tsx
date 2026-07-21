import { useState } from 'react';
import { createReviewProject } from '../api/client';
import type { ReviewProject } from '../types';

interface Props {
  productionId: number;
  /** Documents in the production — bounds the sample-size slider. */
  docCount: number;
  onCreated: (project: ReviewProject) => void;
  onCancel: () => void;
}

const DEFAULT_CATEGORIES = [
  { name: 'relevant', color: 'green', description: 'Supports our case theory or relates to key issues' },
  { name: 'key_document', color: 'blue', description: 'Particularly significant, needs attorney attention' },
  { name: 'not_relevant', color: 'gray', description: 'Not useful to our case' },
  { name: 'needs_review', color: 'yellow', description: 'Ambiguous, attorney should examine manually' },
];

const COLOR_OPTIONS = ['green', 'blue', 'red', 'yellow', 'gray'];

const SWATCH_COLORS: Record<string, string> = {
  green: 'var(--color-success-600)',
  blue: 'var(--color-brand-600)',
  red: 'var(--color-danger-600)',
  yellow: 'var(--color-warning-600)',
  gray: 'var(--color-neutral-500)',
};

export default function ReviewProjectSetup({ productionId, docCount, onCreated, onCancel }: Props) {
  const sampleMin = Math.min(10, Math.max(1, docCount));
  const sampleMax = Math.max(docCount, sampleMin);
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [sampleSize, setSampleSize] = useState(() => Math.min(50, sampleMax));
  const [categories, setCategories] = useState(DEFAULT_CATEGORIES.map(c => ({ ...c })));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleCreate = async () => {
    if (!name.trim() || !prompt.trim()) return;
    setLoading(true);
    setError('');
    try {
      const project = await createReviewProject(productionId, {
        name: name.trim(),
        prompt_text: prompt.trim(),
        sample_size: sampleSize,
        categories: categories.filter(c => c.name.trim()),
      });
      onCreated(project);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create project');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-panel" style={{ width: 600 }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>
            New AI Review Project
          </h3>
          <button className="btn btn-ghost btn-sm" onClick={onCancel}>Close</button>
        </div>

        <div style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
          <div>
            <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Project Name
            </label>
            <input className="input" value={name} onChange={e => setName(e.target.value)}
              placeholder="e.g., Responsiveness Review" />
          </div>

          <div>
            <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Review Criteria
            </label>
            <textarea
              className="input"
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              placeholder="Describe what makes a document responsive. Be specific about parties, topics, date ranges, and document types..."
              rows={8}
              style={{ resize: 'vertical', fontFamily: 'inherit' }}
            />
            <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', marginTop: 'var(--space-1)' }}>
              The AI will use these criteria to classify each document into the categories below.
            </div>
          </div>

          <div>
            <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Categories
            </label>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
              {categories.map((cat, i) => (
                <div key={i} style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
                  <div role="radiogroup" aria-label="Category color" style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                    {COLOR_OPTIONS.map(c => (
                      <button
                        key={c}
                        type="button"
                        role="radio"
                        aria-checked={cat.color === c}
                        aria-label={c}
                        title={c}
                        onClick={() => {
                          const updated = [...categories];
                          updated[i] = { ...updated[i], color: c };
                          setCategories(updated);
                        }}
                        style={{
                          width: 18, height: 18, borderRadius: '50%', cursor: 'pointer', padding: 0,
                          background: SWATCH_COLORS[c],
                          border: cat.color === c ? '2px solid var(--color-ink)' : '2px solid transparent',
                          boxShadow: cat.color === c ? '0 0 0 2px var(--color-card), 0 0 0 3px var(--color-ink)' : 'none',
                        }}
                      />
                    ))}
                  </div>
                  <input className="input" value={cat.name} placeholder="Category name" style={{ width: 140 }}
                    onChange={e => {
                      const updated = [...categories];
                      updated[i] = { ...updated[i], name: e.target.value.toLowerCase().replace(/\s+/g, '_') };
                      setCategories(updated);
                    }} />
                  <input className="input" value={cat.description} placeholder="Description" style={{ flex: 1 }}
                    onChange={e => {
                      const updated = [...categories];
                      updated[i] = { ...updated[i], description: e.target.value };
                      setCategories(updated);
                    }} />
                  <button className="btn btn-ghost btn-sm" style={{ color: 'var(--color-danger-500)', padding: '0 4px' }}
                    onClick={() => setCategories(categories.filter((_, j) => j !== i))}
                    disabled={categories.length <= 2}>
                    x
                  </button>
                </div>
              ))}
              <button className="btn btn-ghost btn-sm" style={{ alignSelf: 'flex-start' }}
                onClick={() => setCategories([...categories, { name: '', color: 'gray', description: '' }])}>
                + Add category
              </button>
            </div>
            <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', marginTop: 'var(--space-1)' }}>
              Customize the classification categories. Names are used as identifiers (lowercase, underscores).
            </div>
          </div>

          <div>
            <label htmlFor="sample-size" style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Sample Size
            </label>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
              <input
                id="sample-size"
                type="range"
                min={sampleMin}
                max={sampleMax}
                value={sampleSize}
                onChange={e => setSampleSize(Number(e.target.value))}
                style={{ flex: 1, accentColor: 'var(--color-ink)' }}
              />
              <span style={{ fontSize: 'var(--text-sm)', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>
                <strong>{sampleSize}</strong>
                <span style={{ color: 'var(--color-neutral-500)' }}> of {docCount} documents</span>
              </span>
            </div>
            <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', marginTop: 'var(--space-1)' }}>
              The AI classifies this many documents first so you can vet its judgment before running the full set.
            </div>
          </div>

          {error && (
            <div style={{ padding: 'var(--space-2) var(--space-3)', fontSize: 'var(--text-sm)', color: 'var(--color-danger-700)', background: 'var(--color-danger-50)', border: '1px solid var(--color-danger-100)', borderRadius: 'var(--radius-md)' }}>
              {error}
            </div>
          )}

          <button className="btn btn-primary" onClick={handleCreate}
            disabled={!name.trim() || !prompt.trim() || loading} style={{ width: '100%' }}>
            {loading ? 'Creating...' : 'Create Project & Run Sample'}
          </button>
        </div>
      </div>
    </div>
  );
}
