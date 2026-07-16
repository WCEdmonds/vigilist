import { useCallback, useEffect, useMemo, useState } from 'react';

const dismissedKey = (uid: string) => `vigilist.onboarding.dismissed.${uid}`;
const seenKey = (uid: string) => `vigilist.onboarding.seen.${uid}`;

/**
 * Storage access that cannot throw. Safari private mode and blocked cookies
 * make even reading the `localStorage` property throw, so the getter is
 * invoked inside the try — never call these with a bare `localStorage` value.
 *
 * A read failure reports "nothing stored", so the guide shows. Preferring to
 * show a guide over crashing the app at boot is deliberate.
 */
function safeGet(getStore: () => Storage, key: string): string | null {
  try {
    return getStore().getItem(key);
  } catch {
    return null;
  }
}

function safeSet(getStore: () => Storage, key: string, value: string): void {
  try {
    getStore().setItem(key, value);
  } catch {
    // Storage unavailable — the preference just won't persist.
  }
}

export function useOnboarding(uid: string | undefined) {
  // Opened by hand via the header button; ignores both storage keys.
  const [forced, setForced] = useState(false);
  // Closed by hand this render; separate from `seen` so closing is instant.
  const [closed, setClosed] = useState(false);

  // Computed once per user. Must not re-run after the effect writes `seen`.
  const shouldAutoOpen = useMemo(() => {
    if (!uid) return false;
    if (safeGet(() => localStorage, dismissedKey(uid))) return false;
    if (safeGet(() => sessionStorage, seenKey(uid))) return false;
    return true;
  }, [uid]);

  // Mark seen as soon as we decide to show it, so a refresh within this
  // session doesn't bring it back.
  useEffect(() => {
    if (uid && shouldAutoOpen) safeSet(() => sessionStorage, seenKey(uid), '1');
  }, [uid, shouldAutoOpen]);

  const close = useCallback(() => {
    setForced(false);
    setClosed(true);
  }, []);

  const dismissForever = useCallback(() => {
    if (uid) safeSet(() => localStorage, dismissedKey(uid), '1');
    setForced(false);
    setClosed(true);
  }, [uid]);

  // Reopening does NOT clear the dismissal — asking to see it once is not
  // asking to have it thrown at you every session again.
  const reopen = useCallback(() => {
    setClosed(false);
    setForced(true);
  }, []);

  const open = forced || (shouldAutoOpen && !closed);

  return { open, close, dismissForever, reopen };
}
