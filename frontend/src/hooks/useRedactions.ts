import { useCallback, useEffect, useState } from 'react';
import { createRedaction, deleteRedaction, listRedactions, updateRedaction } from '../api/client';
import { showToast } from '../components/Toast';
import type { Redaction, RedactionBox, RedactionReason } from '../types';

// The reason picker remembers the last reason used in this browser session.
let lastReason: RedactionReason = 'attorney_client';

type Patch = Partial<Pick<Redaction, 'x_pct' | 'y_pct' | 'w_pct' | 'h_pct' | 'reason_code'>>;

const message = (e: unknown) => (e instanceof Error ? e.message : 'unknown error');

/**
 * A document's redaction boxes with optimistic create / update / delete.
 * A refused save (e.g. someone else's box, read-only access) rolls back and
 * shows the server's reason.
 */
export function useRedactions(docId: string, onChanged?: () => void) {
  // State is tagged with its document, so switching documents shows nothing
  // stale while the new list loads (no synchronous reset in an effect).
  const [state, setState] = useState<{ docId: string; items: Redaction[]; selected: number | null }>(
    { docId, items: [], selected: null });
  const [reason, setReasonState] = useState<RedactionReason>(lastReason);
  const current = state.docId === docId;
  const redactions = current ? state.items : [];
  const selectedId = current ? state.selected : null;

  const setRedactions = useCallback((fn: (prev: Redaction[]) => Redaction[]) => {
    setState(s => (s.docId === docId ? { ...s, items: fn(s.items) } : s));
  }, [docId]);
  const setSelectedId = useCallback((v: number | null | ((prev: number | null) => number | null)) => {
    setState(s => (s.docId === docId
      ? { ...s, selected: typeof v === 'function' ? v(s.selected) : v } : s));
  }, [docId]);

  const reload = useCallback(() => {
    listRedactions(docId)
      .then(items => setState(s => ({ docId, items, selected: s.docId === docId ? s.selected : null })))
      .catch(e => console.warn('listRedactions failed:', e));
  }, [docId]);

  useEffect(() => { reload(); }, [reload]);

  const setReason = useCallback((r: RedactionReason) => {
    lastReason = r;
    setReasonState(r);
  }, []);

  const create = useCallback(async (box: RedactionBox) => {
    const tempId = -Date.now();
    const temp: Redaction = {
      ...box, id: tempId, document_id: docId, note: null, created_by: '',
      created_at: new Date().toISOString(), updated_at: null,
    };
    setRedactions(prev => [...prev, temp]);
    setSelectedId(tempId);
    try {
      const saved = await createRedaction(docId, box);
      setRedactions(prev => prev.map(r => (r.id === tempId ? saved : r)));
      setSelectedId(id => (id === tempId ? saved.id : id));
      onChanged?.();
    } catch (e) {
      setRedactions(prev => prev.filter(r => r.id !== tempId));
      setSelectedId(null);
      showToast(`Redaction not saved: ${message(e)}`, 'error');
    }
  }, [docId, onChanged, setRedactions, setSelectedId]);

  const update = useCallback(async (id: number, patch: Patch) => {
    if (id < 0) return; // still saving
    let before: Redaction | undefined;
    setRedactions(prev => prev.map(r => {
      if (r.id !== id) return r;
      before = r;
      return { ...r, ...patch };
    }));
    try {
      const saved = await updateRedaction(id, patch);
      setRedactions(prev => prev.map(r => (r.id === id ? saved : r)));
      onChanged?.();
    } catch (e) {
      if (before) {
        const restore = before;
        setRedactions(prev => prev.map(r => (r.id === id ? restore : r)));
      }
      showToast(`Change not saved: ${message(e)}`, 'error');
    }
  }, [onChanged, setRedactions]);

  const remove = useCallback(async (id: number) => {
    if (id < 0) return;
    let removed: Redaction | undefined;
    setRedactions(prev => {
      removed = prev.find(r => r.id === id);
      return prev.filter(r => r.id !== id);
    });
    setSelectedId(null);
    try {
      await deleteRedaction(id);
      onChanged?.();
    } catch (e) {
      if (removed) {
        const restore = removed;
        setRedactions(prev => [...prev, restore]);
      }
      showToast(`Redaction not deleted: ${message(e)}`, 'error');
    }
  }, [onChanged, setRedactions, setSelectedId]);

  return { redactions, selectedId, select: setSelectedId, reason, setReason, create, update, remove, reload };
}
