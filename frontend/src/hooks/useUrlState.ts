import { useEffect } from 'react';

/**
 * Very small URL state sync helper.
 *
 * Reads the current query-string at import time into `getInitialUrlState`
 * so components can seed their useState values, and then `syncUrlState`
 * keeps the URL in sync with the current state via history.replaceState
 * (so it survives page reloads without adding navigation history noise).
 */

export interface VigilistUrlState {
  doc?: string;
  q?: string;
  batch?: string;
  view?: string; // 'ai' | etc.
  prod?: string;
}

export function getInitialUrlState(): VigilistUrlState {
  if (typeof window === 'undefined') return {};
  const params = new URLSearchParams(window.location.search);
  const state: VigilistUrlState = {};
  for (const key of ['doc', 'q', 'batch', 'view', 'prod'] as const) {
    const val = params.get(key);
    if (val) state[key] = val;
  }
  return state;
}

function buildSearchString(state: VigilistUrlState): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(state)) {
    if (v) params.set(k, String(v));
  }
  const s = params.toString();
  return s ? `?${s}` : '';
}

/**
 * Pass your current state object; whenever it changes, this hook will
 * `history.replaceState` to keep the URL in sync. It never navigates —
 * it just mirrors state into the address bar so refresh lands back in
 * the same place.
 */
export function useSyncUrl(state: VigilistUrlState) {
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const next = buildSearchString(state);
    const current = window.location.search;
    if (next !== current) {
      const url = window.location.pathname + next + window.location.hash;
      window.history.replaceState(window.history.state, '', url);
    }
  }, [state.doc, state.q, state.batch, state.view, state.prod]);
}
