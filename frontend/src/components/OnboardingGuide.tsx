import { useCallback, useEffect, useRef, useState } from 'react';
import type { Slide } from '../onboarding/slides';

interface Props {
  slides: Slide[];
  onClose: () => void;
  onDismissForever: () => void;
}

export default function OnboardingGuide({ slides, onClose, onDismissForever }: Props) {
  const [index, setIndex] = useState(0);
  const [dontShow, setDontShow] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  const slide = slides[index];
  const isLast = index === slides.length - 1;

  // Every exit path funnels through here so the checkbox is always honored.
  const finish = useCallback(() => {
    if (dontShow) onDismissForever();
    else onClose();
  }, [dontShow, onClose, onDismissForever]);

  // Esc closes, same as the X.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') finish();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [finish]);

  // Move focus into the dialog on open, and hand it back on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    return () => previous?.focus?.();
  }, []);

  if (!slide) return null;

  return (
    <div className="modal-overlay" onClick={finish}>
      <div
        ref={panelRef}
        className="modal-panel onboarding-panel"
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        tabIndex={-1}
      >
        <div className="modal-header">
          <h2 id="onboarding-title" className="modal-title">
            {slide.title}
          </h2>
          <button className="modal-close-btn" aria-label="Close guide" onClick={finish}>
            &times;
          </button>
        </div>

        <div className="modal-body">
          <div className="onboarding-icon" aria-hidden="true">{slide.icon}</div>
          <div className="onboarding-body">{slide.body}</div>
        </div>

        <div className="modal-footer">
          <label className="onboarding-dont-show">
            <input
              type="checkbox"
              checked={dontShow}
              onChange={e => setDontShow(e.target.checked)}
            />
            Don&apos;t show again
          </label>

          {/* Plain buttons, not role="tab" — ARIA tabs require a matching
              tabpanel and aria-controls, which these dots don't have. */}
          <div className="onboarding-dots">
            {slides.map((s, i) => (
              <button
                key={s.id}
                type="button"
                aria-current={i === index ? 'true' : undefined}
                aria-label={`Slide ${i + 1} of ${slides.length}: ${s.title}`}
                className={`onboarding-dot ${i === index ? 'active' : ''}`}
                onClick={() => setIndex(i)}
              />
            ))}
          </div>

          <div className="onboarding-nav">
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setIndex(i => i - 1)}
              disabled={index === 0}
            >
              Back
            </button>
            {isLast ? (
              <button className="btn btn-primary btn-sm" onClick={finish}>
                Done
              </button>
            ) : (
              <button className="btn btn-primary btn-sm" onClick={() => setIndex(i => i + 1)}>
                Next
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
