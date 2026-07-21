import { useCallback, useEffect, useState } from 'react';

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

export interface OnboardingState {
  open: boolean;
  /** Close for this session only. */
  close: () => void;
  /** Tick "Don't show again" and close. Permanent. */
  dismissForever: () => void;
  /** Force open from the header button, ignoring both storage keys. */
  reopen: () => void;
}

function computeShouldAutoOpen(uid: string | undefined): boolean {
  if (!uid) return false;
  if (safeGet(() => localStorage, dismissedKey(uid)) !== null) return false;
  if (safeGet(() => sessionStorage, seenKey(uid)) !== null) return false;
  return true;
}

/**
 * Decides whether the onboarding guide should be showing, and owns the only
 * two storage keys involved.
 *
 * PRECONDITION: this hook must be mounted under a component that remounts when
 * the signed-in user changes. `AppRouter` satisfies this — it is gated on
 * `user` and keyed by uid. The auto-open decision is captured once at mount by
 * a `useState` lazy initializer; if the hook were mounted somewhere that
 * survived a user change, a second user would inherit the first user's
 * decision. Do not move it above the `!user` gate in `AppContent`.
 */
export function useOnboarding(uid: string | undefined): OnboardingState {
  // Keyed by uid rather than plain booleans, so that even within one mount a
  // stale flag cannot apply to a different user.
  const [closedFor, setClosedFor] = useState<string | undefined>(undefined);
  const [forcedFor, setForcedFor] = useState<string | undefined>(undefined);

  // Decided exactly once per mount. A lazy initializer is a React semantic
  // guarantee; useMemo would NOT be — React may discard a memo cache, and a
  // recompute after the effect below writes `seen` would flip this to false
  // and close the modal mid-read.
  const [shouldAutoOpen] = useState(() => computeShouldAutoOpen(uid));

  // Mark seen as soon as we decide to show it, so a refresh within this
  // session doesn't bring it back.
  useEffect(() => {
    if (uid && shouldAutoOpen) safeSet(() => sessionStorage, seenKey(uid), '1');
  }, [uid, shouldAutoOpen]);

  const close = useCallback(() => {
    setForcedFor(undefined);
    setClosedFor(uid);
  }, [uid]);

  const dismissForever = useCallback(() => {
    if (uid) safeSet(() => localStorage, dismissedKey(uid), '1');
    setForcedFor(undefined);
    setClosedFor(uid);
  }, [uid]);

  // Reopening does NOT clear the dismissal — asking to see it once is not
  // asking to have it thrown at you every session again.
  const reopen = useCallback(() => {
    setClosedFor(undefined);
    setForcedFor(uid);
  }, [uid]);

  const closed = uid !== undefined && closedFor === uid;
  const forced = uid !== undefined && forcedFor === uid;
  const open = forced || (shouldAutoOpen && !closed);

  return { open, close, dismissForever, reopen };
}
