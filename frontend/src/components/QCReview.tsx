import { useCallback, useEffect, useState } from 'react';
import { getQCContext, recordQCDecision, getTags } from '../api/client';
import type { QCContext, Tag } from '../types';
import DocumentViewer from './DocumentViewer';

interface Props {
  sampleIds: number[];
  productionId: number;
  onClose: () => void;
}

export default function QCReview({ sampleIds, onClose }: Props) {
  const [currentIndex, setCurrentIndex] = useState(0);
  const [context, setContext] = useState<QCContext | null>(null);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState('');

  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [tagsLoaded, setTagsLoaded] = useState(false);

  // Overturn form state
  const [showOverturn, setShowOverturn] = useState(false);
  const [overturnReason, setOverturnReason] = useState('');
  const [selectedTagIds, setSelectedTagIds] = useState<Set<number>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');

  const [done, setDone] = useState(false);

  const currentBdId = sampleIds[currentIndex] ?? null;

  const loadContext = useCallback((bdId: number) => {
    setContextLoading(true);
    setContextError('');
    setContext(null);
    setShowOverturn(false);
    setOverturnReason('');
    setSubmitError('');
    let cancelled = false;
    getQCContext(bdId)
      .then(ctx => {
        if (cancelled) return;
        setContext(ctx);
        setSelectedTagIds(new Set(ctx.current_tags.map(t => t.id)));
      })
      .catch(e => {
        if (cancelled) return;
        setContextError(e instanceof Error ? e.message : 'Failed to load QC context');
      })
      .finally(() => {
        if (!cancelled) setContextLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  // Load tags once
  useEffect(() => {
    if (tagsLoaded) return;
    getTags()
      .then(tags => {
        setAllTags(tags);
        setTagsLoaded(true);
      })
      .catch(() => {
        // Non-fatal: overturn tag picker will be empty
        setTagsLoaded(true);
      });
  }, [tagsLoaded]);

  // Load context whenever index changes
  useEffect(() => {
    if (currentBdId === null) return;
    const cleanup = loadContext(currentBdId);
    return cleanup;
  }, [currentBdId, loadContext]);

  const advance = useCallback(() => {
    const nextIndex = currentIndex + 1;
    if (nextIndex >= sampleIds.length) {
      setDone(true);
    } else {
      setCurrentIndex(nextIndex);
    }
  }, [currentIndex, sampleIds.length]);

  const handleAgree = async () => {
    if (!currentBdId || submitting) return;
    setSubmitting(true);
    setSubmitError('');
    try {
      await recordQCDecision(currentBdId, 'agree');
      advance();
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : 'Failed to record decision');
    } finally {
      setSubmitting(false);
    }
  };

  const handleConfirmOverturn = async () => {
    if (!currentBdId || submitting) return;
    if (!overturnReason.trim()) {
      setSubmitError('A reason is required to overturn.');
      return;
    }
    setSubmitting(true);
    setSubmitError('');
    try {
      await recordQCDecision(currentBdId, 'overturn', overturnReason.trim(), Array.from(selectedTagIds));
      advance();
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : 'Failed to record decision');
    } finally {
      setSubmitting(false);
    }
  };

  const toggleTag = (tagId: number) => {
    setSelectedTagIds(prev => {
      const next = new Set(prev);
      if (next.has(tagId)) {
        next.delete(tagId);
      } else {
        next.add(tagId);
      }
      return next;
    });
  };

  const progressPercent = sampleIds.length > 0
    ? Math.round(((currentIndex + 1) / sampleIds.length) * 100)
    : 0;

  if (done) {
    return (
      <div style={{ position: 'fixed', inset: 0, zIndex: 200, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'var(--color-neutral-50)' }}>
        <div style={{ textAlign: 'center', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 'var(--space-4)' }}>
          <div style={{ fontSize: 'var(--text-xl)', fontFamily: 'var(--font-serif)', color: 'var(--color-neutral-700)' }}>
            QC Review Complete
          </div>
          <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)' }}>
            All {sampleIds.length} sampled document{sampleIds.length !== 1 ? 's' : ''} have been QC'd.
          </div>
          <button className="btn btn-primary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 200, display: 'flex', flexDirection: 'column', background: 'var(--color-neutral-50)' }}>
      {/* Header */}
      <div className="fullscreen-bar">
        <span className="fs-title">
          QC Review: {currentIndex + 1} of {sampleIds.length}
        </span>
        {/* Progress bar */}
        <div className="qc-progress-track">
          <div
            style={{
              height: '100%',
              width: `${progressPercent}%`,
              background: 'var(--color-primary-300)',
              borderRadius: 3,
              transition: 'width 0.3s',
            }}
          />
        </div>
        <span className="fs-progress">{progressPercent}%</span>
        <button
          className="cb-action"
          style={{ marginLeft: 'auto' }}
          onClick={onClose}
        >
          Close
        </button>
      </div>

      {/* Body */}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Left panel */}
        <div style={{ width: 320, overflowY: 'auto', borderRight: '1px solid var(--color-neutral-200)', padding: 'var(--space-4)', flexShrink: 0, background: 'white', display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>

          {contextLoading && (
            <div className="loading-center" style={{ padding: 'var(--space-6)' }}>
              <span className="spinner spinner-md" />
              <span style={{ marginLeft: 'var(--space-2)' }}>Loading QC context…</span>
            </div>
          )}

          {contextError && (
            <div style={{ color: 'var(--color-danger-700)', fontSize: 'var(--text-sm)', background: 'var(--color-danger-50)', padding: 'var(--space-3)', borderRadius: 'var(--radius-md)' }}>
              {contextError}
              <button
                className="btn btn-ghost btn-sm"
                style={{ marginTop: 'var(--space-2)', display: 'block' }}
                onClick={() => currentBdId !== null && loadContext(currentBdId)}
              >
                Retry
              </button>
            </div>
          )}

          {context && !contextLoading && (
            <>
              {/* Document info */}
              <div>
                <div className="qc-section-label">
                  Document
                </div>
                <div style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--text-sm)', fontWeight: 600, color: 'var(--color-neutral-800)' }}>
                  {context.bates_begin}
                </div>
                {context.title && (
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginTop: 2 }}>
                    {context.title}
                  </div>
                )}
              </div>

              {/* Original reviewer */}
              <div>
                <div className="qc-section-label">
                  Original Reviewer
                </div>
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-700)' }}>
                  {context.original_reviewer_email ?? context.original_reviewer_id}
                </div>
              </div>

              {/* Current tags */}
              <div>
                <div className="qc-section-label">
                  Current Tags
                </div>
                {context.current_tags.length === 0 ? (
                  <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)' }}>No tags applied</span>
                ) : (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-1)' }}>
                    {context.current_tags.map(tag => (
                      <span
                        key={tag.id}
                        className="badge badge-blue"
                        style={{ fontSize: 'var(--text-xs)' }}
                        title={tag.category}
                      >
                        {tag.name}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              {/* Already QC'd notice */}
              {context.existing_decision && (
                <div style={{ background: 'var(--color-neutral-100)', border: '1px solid var(--color-neutral-300)', borderRadius: 'var(--radius-md)', padding: 'var(--space-3)', opacity: 0.75 }}>
                  <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-600)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 'var(--space-1)' }}>
                    Already QC'd
                  </div>
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-700)' }}>
                    Decision: <strong style={{ textTransform: 'capitalize' }}>{context.existing_decision.decision}</strong>
                  </div>
                  {context.existing_decision.reason && (
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)', marginTop: 4 }}>
                      Reason: {context.existing_decision.reason}
                    </div>
                  )}
                  <button
                    className="btn btn-ghost btn-sm"
                    style={{ marginTop: 'var(--space-2)' }}
                    onClick={advance}
                  >
                    Skip
                  </button>
                </div>
              )}

              {/* Action area */}
              {submitError && (
                <div style={{ color: 'var(--color-danger-700)', fontSize: 'var(--text-xs)', background: 'var(--color-danger-50)', padding: 'var(--space-2)', borderRadius: 'var(--radius-md)' }}>
                  {submitError}
                </div>
              )}

              {!showOverturn ? (
                <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={submitting}
                    onClick={handleAgree}
                    style={{ flex: 1 }}
                  >
                    {submitting ? 'Saving...' : 'Agree'}
                  </button>
                  <button
                    className="btn btn-secondary btn-sm"
                    disabled={submitting}
                    onClick={() => { setShowOverturn(true); setSubmitError(''); }}
                    style={{ flex: 1 }}
                  >
                    Overturn
                  </button>
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)', border: '1px solid var(--color-danger-300)', borderRadius: 'var(--radius-md)', padding: 'var(--space-3)', background: 'var(--color-danger-50)' }}>
                  <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-danger-700)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                    Overturn — Required Fields
                  </div>
                  <div>
                    <label style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)', display: 'block', marginBottom: 4 }}>
                      Reason *
                    </label>
                    <textarea
                      className="input"
                      rows={3}
                      value={overturnReason}
                      onChange={e => setOverturnReason(e.target.value)}
                      placeholder="Explain why you are overturning this coding decision..."
                      style={{ width: '100%', fontSize: 'var(--text-xs)', resize: 'vertical', boxSizing: 'border-box' }}
                    />
                  </div>

                  {tagsLoaded && allTags.length > 0 && (
                    <div>
                      <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)', marginBottom: 4 }}>
                        Update Tags
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 180, overflowY: 'auto' }}>
                        {allTags.map(tag => (
                          <label
                            key={tag.id}
                            style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', cursor: 'pointer', fontSize: 'var(--text-xs)' }}
                          >
                            <input
                              type="checkbox"
                              checked={selectedTagIds.has(tag.id)}
                              onChange={() => toggleTag(tag.id)}
                            />
                            <span>{tag.name}</span>
                            <span style={{ color: 'var(--color-neutral-400)', fontSize: 10 }}>{tag.category}</span>
                          </label>
                        ))}
                      </div>
                    </div>
                  )}

                  <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                    <button
                      className="btn btn-sm"
                      style={{ background: 'var(--color-danger-600)', color: 'white', flex: 1 }}
                      disabled={submitting || !overturnReason.trim()}
                      onClick={handleConfirmOverturn}
                    >
                      {submitting ? 'Saving...' : 'Confirm Overturn'}
                    </button>
                    <button
                      className="btn btn-ghost btn-sm"
                      disabled={submitting}
                      onClick={() => { setShowOverturn(false); setSubmitError(''); }}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Right panel: DocumentViewer */}
        <div style={{ flex: 1, overflow: 'hidden' }}>
          {context ? (
            <DocumentViewer
              docId={context.document_id}
              onNavigate={() => {}}
              onBack={() => {}}
              searchQuery=""
              onSearch={() => {}}
            />
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--color-neutral-400)', fontSize: 'var(--text-sm)' }}>
              {contextLoading ? '' : 'No document loaded'}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
