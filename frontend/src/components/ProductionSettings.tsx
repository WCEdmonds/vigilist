import { useCallback, useEffect, useRef, useState } from 'react';
import { updateProduction } from '../api/client';
import { showToast } from './Toast';
import type { ProductionInfo } from '../types';

interface Props {
  production: ProductionInfo;
  onClose: () => void;
  onSaved: (p: ProductionInfo) => void;
}

export default function ProductionSettings({ production, onClose, onSaved }: Props) {
  const [description, setDescription] = useState(() => production.description ?? '');
  const [caseContext, setCaseContext] = useState(() => production.case_context ?? '');
  const [saving, setSaving] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // Esc closes, same as the X — no-op while a save is in flight.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, saving]);

  // Move focus into the dialog on open, and hand it back on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    return () => previous?.focus?.();
  }, []);

  const handleClose = useCallback(() => {
    if (saving) return;
    onClose();
  }, [onClose, saving]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const updated = await updateProduction(production.id, {
        description: description.trim(),
        case_context: caseContext.trim(),
      });
      showToast('Settings saved', 'success');
      onSaved(updated);
    } catch (e: unknown) {
      showToast(e instanceof Error ? e.message : 'Failed to save settings', 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div
        ref={panelRef}
        className="modal-panel"
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="production-settings-title"
        tabIndex={-1}
      >
        <div className="modal-header">
          <h3 id="production-settings-title" className="modal-title">
            Production settings
          </h3>
          <button className="modal-close-btn" aria-label="Close" onClick={handleClose}>
            &times;
          </button>
        </div>

        <div className="modal-body">
          <div style={{ marginBottom: 'var(--space-4)' }}>
            <label className="input-label" htmlFor="settings-description">
              Description
            </label>
            <input
              id="settings-description"
              className="input"
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Brief description"
              disabled={saving}
            />
          </div>

          <div>
            <label className="input-label" htmlFor="settings-case-context">
              About this case <span className="brief-ai-mark">✦</span>
            </label>
            <p className="input-hint">
              A few sentences: what the case is about and what makes a document
              relevant. The AI uses this to brief your team and, later, to
              classify documents. You can edit it anytime in Production settings.
            </p>
            <textarea
              id="settings-case-context"
              className="input"
              rows={4}
              value={caseContext}
              onChange={e => setCaseContext(e.target.value)}
              placeholder="e.g. Product-liability suit over the March 2024 recall. Relevant: anything about the recall decision, board discussions, or customer injuries."
              disabled={saving}
            />
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn btn-ghost btn-sm" onClick={handleClose} disabled={saving}>
            Cancel
          </button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
