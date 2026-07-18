import { useCallback, useEffect, useRef, useState } from 'react';
import {
  bulkAcceptResults, createQueue, deleteReviewProject, getProjectStatus, listReviewProjects, listReviewResults,
  pauseRun, recordDecision, runFull, runSample, updateReviewProject,
} from '../api/client';
import type { AIReviewResult, PaginatedReviewResults, ReviewProject } from '../types';
import ReviewProjectSetup from './ReviewProjectSetup';
import { showToast } from './Toast';

interface Props {
  productionId: number;
  onViewDocument: (docId: string, excerpts?: string[]) => void;
  /** Called after a queue is successfully created from a results slice. */
  onQueueCreated?: () => void;
}

const CAT_COLORS: Record<string, string> = {
  green: 'var(--color-success-600)',
  blue: 'var(--color-brand-600)',
  red: 'var(--color-danger-600)',
  yellow: 'var(--color-warning-600)',
  gray: 'var(--color-neutral-500)',
};

function getCategoryStyle(categories: { name: string; color: string; description: string }[], decision: string) {
  const cat = categories.find(c => c.name === decision);
  return {
    color: CAT_COLORS[cat?.color || 'gray'] || 'var(--color-neutral-500)',
    label: cat?.name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) || decision,
  };
}

export default function AIReviewLane({ productionId, onViewDocument, onQueueCreated }: Props) {
  const [projects, setProjects] = useState<ReviewProject[]>([]);
  const [activeProjectId, setActiveProjectId] = useState<number | null>(null);
  const [activeProject, setActiveProject] = useState<ReviewProject | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  const [results, setResults] = useState<PaginatedReviewResults | null>(null);
  const [selectedResult, setSelectedResult] = useState<AIReviewResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [sort, setSort] = useState('confidence_asc');
  const [decisionNote, setDecisionNote] = useState('');
  const pollRef = useRef<number | null>(null);

  // Bulk accept
  const [bulkThreshold, setBulkThreshold] = useState(80);
  const [bulkAccepting, setBulkAccepting] = useState(false);

  // Queue from this slice
  const [showQueueForm, setShowQueueForm] = useState(false);
  const [queueDecision, setQueueDecision] = useState('relevant');
  const [queueName, setQueueName] = useState('AI relevant ≥80%');
  const [queueCreating, setQueueCreating] = useState(false);

  // Load projects
  useEffect(() => {
    listReviewProjects(productionId).then(setProjects).catch(() => {});
  }, [productionId]);

  // Sync activeProject from projects list when activeProjectId changes
  useEffect(() => {
    if (!activeProjectId) { setActiveProject(null); return; }
    const proj = projects.find(p => p.id === activeProjectId);
    if (proj) setActiveProject(proj);
  }, [activeProjectId, projects]);

  // Load results when active project or sort changes — use stable ID, not object ref
  const fetchResults = useCallback(async () => {
    if (!activeProject) { setResults(null); return; }
    setLoading(true);
    try {
      const res = await listReviewResults(
        productionId, activeProject.id, 1, 200, sort,
        { sample_only: activeProject.status === 'reviewing_sample' }
      );
      setResults(res);
    } finally {
      setLoading(false);
    }
  }, [activeProjectId, activeProject?.status, sort, productionId]);

  useEffect(() => { fetchResults(); }, [fetchResults]);

  // Poll status when running/sampling — use ref to avoid re-render loops
  useEffect(() => {
    if (!activeProject || !['sampling', 'running'].includes(activeProject.status)) {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }

    const projectId = activeProject.id;

    pollRef.current = window.setInterval(async () => {
      try {
        const status = await getProjectStatus(productionId, projectId);
        // Only update the progress fields, don't replace the whole object
        setActiveProject(prev => prev && prev.id === projectId ? {
          ...prev,
          status: status.status,
          processed_documents: status.processed_documents,
          total_documents: status.total_documents,
          total_cost_tokens: status.total_cost_tokens,
        } : prev);

        if (['reviewing_sample', 'complete'].includes(status.status)) {
          if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
          // Refresh everything
          const updated = await listReviewProjects(productionId);
          setProjects(updated);
          // Results will auto-refresh via the useEffect above since status changed
        }
      } catch { /* ignore */ }
    }, 3000);

    return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
  }, [activeProjectId, activeProject?.status, productionId]);

  const handleSelectProject = (p: ReviewProject) => {
    setActiveProjectId(p.id);
    setSelectedResult(null);
  };

  const handleProjectCreated = async (project: ReviewProject) => {
    setShowSetup(false);
    setProjects(prev => [project, ...prev]);
    setActiveProjectId(project.id);
    setActiveProject(project);
    // Auto-run sample
    await runSample(productionId, project.id);
    setActiveProject(prev => prev ? { ...prev, status: 'sampling' } : prev);
  };

  const handleRunFull = async () => {
    if (!activeProject) return;
    await runFull(productionId, activeProject.id);
    setActiveProject(prev => prev ? { ...prev, status: 'running' } : prev);
  };

  const handlePause = async () => {
    if (!activeProject) return;
    await pauseRun(productionId, activeProject.id);
    setActiveProject(prev => prev ? { ...prev, status: 'paused' } : prev);
  };

  const handleDecision = async (decision: string) => {
    if (!selectedResult) return;
    try {
      const updated = await recordDecision(selectedResult.id, decision, decisionNote || undefined);
      setResults(prev => prev ? {
        ...prev,
        results: prev.results.map(r => r.id === updated.id ? updated : r),
      } : prev);
      setSelectedResult(updated);
      setDecisionNote('');
      // Auto-advance to next unreviewed
      if (results) {
        const nextIdx = results.results.findIndex(r => r.id === selectedResult.id) + 1;
        const next = results.results.slice(nextIdx).find(r => !r.attorney_decision);
        if (next) setSelectedResult(next);
      }
    } catch {
      showToast('Could not record decision', 'error');
    }
  };

  const handleMakePrimary = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation();
    try {
      await updateReviewProject(productionId, id, { is_primary: true });
      const updated = await listReviewProjects(productionId);
      setProjects(updated);
      showToast('Primary project updated', 'success');
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Could not update primary project', 'error');
    }
  };

  const handleDelete = async (id: number) => {
    await deleteReviewProject(productionId, id);
    setProjects(prev => prev.filter(p => p.id !== id));
    if (activeProjectId === id) { setActiveProjectId(null); setResults(null); setSelectedResult(null); }
  };

  const handleBulkAccept = async () => {
    if (!activeProject) return;
    setBulkAccepting(true);
    try {
      const { accepted } = await bulkAcceptResults(productionId, activeProject.id, bulkThreshold);
      showToast(`Accepted ${accepted} suggestions`, 'success');
      await fetchResults();
      const updated = await listReviewProjects(productionId);
      setProjects(updated);
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Bulk accept failed', 'error');
    } finally {
      setBulkAccepting(false);
    }
  };

  const openQueueForm = () => {
    const decision = queueDecision || 'relevant';
    setQueueName(`AI ${decision} ≥80%`);
    setShowQueueForm(true);
  };

  const handleQueueDecisionChange = (decision: string) => {
    setQueueDecision(decision);
    setQueueName(`AI ${decision} ≥80%`);
  };

  const handleCreateQueueFromSlice = async () => {
    if (!activeProject) return;
    setQueueCreating(true);
    try {
      await createQueue(productionId, queueName.trim() || `AI ${queueDecision} ≥80%`, '', '', {
        ai: { project_id: activeProject.id, decision: queueDecision, min_confidence: 80, exclude_decided: true },
      });
      showToast(`Queue "${queueName.trim() || queueDecision}" created`, 'success');
      setShowQueueForm(false);
      onQueueCreated?.();
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Could not create queue', 'error');
    } finally {
      setQueueCreating(false);
    }
  };

  const isProcessing = activeProject && ['sampling', 'running'].includes(activeProject.status);
  const progressPct = activeProject && activeProject.total_documents > 0
    ? Math.round((activeProject.processed_documents / activeProject.total_documents) * 100) : 0;

  // Compute agreement rate from all results (not just current page)
  const agreementRate = results?.agreement_rate;

  return (
    <div className="review-lane" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Lane toolbar */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: 'var(--space-2) var(--space-3)', borderBottom: '1px solid var(--color-neutral-200)',
      }}>
        <span style={{ fontSize: 'var(--text-sm)', fontWeight: 600, color: 'var(--color-neutral-500)' }}>Smart Review</span>
        <button className="btn btn-primary btn-sm" onClick={() => setShowSetup(true)}>
          + New Review Project
        </button>
      </div>

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Left: Project list */}
        <div style={{ width: 280, borderRight: '1px solid var(--color-neutral-200)', overflow: 'auto', padding: 'var(--space-3)' }}>
          <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 'var(--space-2)' }}>
            Review Projects
          </div>
          {projects.map(p => (
            <div key={p.id} onClick={() => handleSelectProject(p)} style={{
              padding: 'var(--space-2) var(--space-3)', borderRadius: 'var(--radius-md)', cursor: 'pointer',
              background: activeProjectId === p.id ? 'var(--color-neutral-100)' : 'transparent',
              marginBottom: 'var(--space-1)',
            }}>
              <div style={{ fontSize: 'var(--text-sm)', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                {p.name}
                {p.is_primary && <span className="badge badge-blue">Primary</span>}
              </div>
              <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                <span style={{ textTransform: 'capitalize' }}>{p.status.replace(/_/g, ' ')}</span>
                {p.decision_breakdown && (
                  <span>{Object.values(p.decision_breakdown).reduce((a, b) => a + b, 0)} docs</span>
                )}
                {!p.is_primary && (
                  <button type="button" className="btn btn-ghost btn-xs" onClick={e => handleMakePrimary(e, p.id)}>
                    Make primary
                  </button>
                )}
              </div>
            </div>
          ))}
          {projects.length === 0 && (
            <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-400)', padding: 'var(--space-4)', textAlign: 'center' }}>
              No review projects yet
            </div>
          )}
        </div>

        {/* Center: Results queue */}
        <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-3)' }}>
          {!activeProject ? (
            <div style={{ textAlign: 'center', padding: 'var(--space-8)', color: 'var(--color-neutral-400)' }}>
              Select a review project or create a new one
            </div>
          ) : (
            <>
              {/* Project header */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', marginBottom: 'var(--space-3)' }}>
                <div style={{ flex: 1 }}>
                  <h2 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>{activeProject.name}</h2>
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)' }}>
                    {activeProject.processed_documents} / {activeProject.total_documents} documents
                    {activeProject.total_cost_tokens > 0 && ` · ${(activeProject.total_cost_tokens / 1000).toFixed(1)}K tokens`}
                    {agreementRate != null && ` · ${Math.round(agreementRate * 100)}% agreement`}
                    {activeProject.sample_agreement_rate != null && agreementRate == null && ` · ${Math.round(activeProject.sample_agreement_rate * 100)}% agreement`}
                  </div>
                </div>
                {activeProject.status === 'reviewing_sample' && (
                  <button className="btn btn-primary btn-sm" onClick={handleRunFull}>
                    Run Full Corpus
                  </button>
                )}
                {isProcessing && (
                  <button className="btn btn-secondary btn-sm" onClick={handlePause}>Pause</button>
                )}
                {activeProject.status === 'paused' && (
                  <button className="btn btn-primary btn-sm" onClick={handleRunFull}>Resume</button>
                )}
                <button className="btn btn-ghost btn-sm" style={{ color: 'var(--color-danger-500)' }}
                  onClick={() => handleDelete(activeProject.id)}>Delete</button>
              </div>

              {/* Results header: bulk accept + queue-from-slice */}
              <div style={{
                display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 'var(--space-3)',
                padding: 'var(--space-2) var(--space-3)', marginBottom: 'var(--space-3)',
                background: 'var(--color-neutral-50)', border: '1px solid var(--color-neutral-200)', borderRadius: 'var(--radius-md)',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
                  <span>Bulk accept &ge;</span>
                  <input
                    type="number" className="input" min={0} max={100}
                    value={bulkThreshold}
                    onChange={e => setBulkThreshold(Math.max(1, Number(e.target.value) || 0))}
                    style={{ width: 64, padding: '2px 6px' }}
                  />
                  <span>%</span>
                  <button className="btn btn-secondary btn-sm" disabled={bulkAccepting} onClick={handleBulkAccept}>
                    {bulkAccepting ? 'Accepting…' : 'Bulk accept'}
                  </button>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                  {!showQueueForm ? (
                    <button className="btn btn-ghost btn-sm" onClick={openQueueForm}>
                      &rarr; Queue from this slice
                    </button>
                  ) : (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--text-sm)' }}>
                      <select className="input" value={queueDecision} onChange={e => handleQueueDecisionChange(e.target.value)} style={{ padding: '2px 6px' }}>
                        {activeProject.categories.map(c => (
                          <option key={c.name} value={c.name}>{c.name}</option>
                        ))}
                        {!activeProject.categories.some(c => c.name === 'relevant') && (
                          <option value="relevant">relevant</option>
                        )}
                      </select>
                      <input
                        type="text" className="input" value={queueName}
                        onChange={e => setQueueName(e.target.value)}
                        style={{ width: 200, padding: '2px 6px' }}
                      />
                      <button className="btn btn-primary btn-sm" disabled={queueCreating} onClick={handleCreateQueueFromSlice}>
                        {queueCreating ? 'Creating…' : 'Create'}
                      </button>
                      <button className="btn btn-ghost btn-sm" onClick={() => setShowQueueForm(false)}>Cancel</button>
                    </div>
                  )}
                </div>
              </div>

              {/* Progress bar */}
              {isProcessing && (
                <div style={{ height: 4, background: 'var(--color-neutral-200)', borderRadius: 2, marginBottom: 'var(--space-3)' }}>
                  <div style={{ height: '100%', width: `${progressPct}%`, background: 'var(--color-brand-500)', borderRadius: 2, transition: 'width 0.3s' }} />
                </div>
              )}

              {/* Sort controls */}
              <div style={{ display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-2)', fontSize: 'var(--text-xs)' }}>
                {[
                  { value: 'confidence_asc', label: 'Least confident' },
                  { value: 'confidence_desc', label: 'Most confident' },
                  { value: 'decision', label: 'By decision' },
                ].map(s => (
                  <button key={s.value} onClick={() => setSort(s.value)}
                    className={`btn btn-sm ${sort === s.value ? 'btn-secondary' : 'btn-ghost'}`}>
                    {s.label}
                  </button>
                ))}
              </div>

              {/* Results list */}
              {loading ? (
                <div className="loading-center"><span className="spinner spinner-md" /> Running AI review…</div>
              ) : results?.results.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 'var(--space-8)', color: 'var(--color-neutral-400)' }}>
                  {isProcessing ? 'Processing documents...' : 'No results yet'}
                </div>
              ) : results?.results.map(r => (
                <div key={r.id} onClick={() => setSelectedResult(r)} style={{
                  padding: 'var(--space-2) var(--space-3)', borderRadius: 'var(--radius-md)', cursor: 'pointer',
                  border: '1px solid var(--color-neutral-200)', marginBottom: 'var(--space-1-5)',
                  background: selectedResult?.id === r.id ? 'var(--color-neutral-50)' : 'white',
                  display: 'flex', alignItems: 'center', gap: 'var(--space-3)',
                }}>
                  <span style={{
                    fontSize: 'var(--text-xs)', fontWeight: 700, padding: '2px 8px', borderRadius: 'var(--radius-sm)',
                    color: '#fff', background: getCategoryStyle(activeProject.categories, r.ai_decision).color,
                    whiteSpace: 'nowrap',
                  }}>
                    {getCategoryStyle(activeProject.categories, r.ai_decision).label}
                  </span>
                  <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', width: 30, textAlign: 'center' }}>
                    {r.confidence_score}%
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 'var(--text-sm)', fontWeight: 500 }}>{r.title || r.bates_begin}</div>
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {r.reasoning}
                    </div>
                  </div>
                  {r.attorney_decision && (
                    <span style={{ fontSize: 'var(--text-xs)', color: r.attorney_decision === 'agree' ? 'var(--color-success-600)' : 'var(--color-warning-600)' }}>
                      {r.attorney_decision === 'agree' ? '✓' : '✎'}
                    </span>
                  )}
                </div>
              ))}
            </>
          )}
        </div>

        {/* Right: Review panel */}
        {selectedResult && activeProject && (
          <div style={{ width: 400, borderLeft: '1px solid var(--color-neutral-200)', overflow: 'auto', padding: 'var(--space-3)', display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
            {/* AI Decision */}
            <div>
              <span style={{
                fontSize: 'var(--text-sm)', fontWeight: 700, padding: '4px 12px', borderRadius: 'var(--radius-md)',
                color: '#fff', background: getCategoryStyle(activeProject.categories, selectedResult.ai_decision).color,
              }}>
                {getCategoryStyle(activeProject.categories, selectedResult.ai_decision).label} — {selectedResult.confidence_score}%
              </span>
            </div>

            {/* Document info */}
            <div>
              <div style={{ fontSize: 'var(--text-sm)', fontWeight: 600 }}>{selectedResult.title || selectedResult.bates_begin}</div>
              <button className="btn btn-ghost btn-sm" style={{ padding: 0, fontSize: 'var(--text-xs)' }}
                onClick={() => onViewDocument(selectedResult.document_id, selectedResult.key_excerpts.map(e => e.text))}>
                View document →
              </button>
            </div>

            {/* Reasoning */}
            <div>
              <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', marginBottom: 'var(--space-1)' }}>Reasoning</div>
              <div style={{ fontSize: 'var(--text-sm)', lineHeight: 1.5 }}>{selectedResult.reasoning}</div>
            </div>

            {/* Key excerpts */}
            {selectedResult.key_excerpts.length > 0 && (
              <div>
                <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', marginBottom: 'var(--space-1)' }}>Key Excerpts</div>
                {selectedResult.key_excerpts.map((ex, i) => (
                  <div key={i} onClick={() => onViewDocument(selectedResult.document_id, [ex.text])} style={{
                    fontSize: 'var(--text-sm)', padding: 'var(--space-2)', background: 'var(--color-warning-50)',
                    borderLeft: '3px solid var(--color-warning-400)', borderRadius: 'var(--radius-sm)',
                    marginBottom: 'var(--space-1)', cursor: 'pointer',
                  }}>
                    "{ex.text}"
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-warning-600)', marginTop: 2 }}>Click to view in document</div>
                  </div>
                ))}
              </div>
            )}

            {/* Considerations */}
            {selectedResult.considerations && (
              <div>
                <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', marginBottom: 'var(--space-1)' }}>Considerations</div>
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-600)', fontStyle: 'italic' }}>{selectedResult.considerations}</div>
              </div>
            )}

            {/* Attorney decision */}
            <div style={{ borderTop: '1px solid var(--color-neutral-200)', paddingTop: 'var(--space-3)' }}>
              {selectedResult.attorney_decision ? (
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)' }}>
                  Decision: <strong>{selectedResult.attorney_decision.replace(/_/g, ' ')}</strong>
                  {selectedResult.attorney_note && <div style={{ marginTop: 'var(--space-1)' }}>Note: {selectedResult.attorney_note}</div>}
                </div>
              ) : (
                <>
                  <textarea className="input" placeholder="Optional note..." rows={2}
                    value={decisionNote} onChange={e => setDecisionNote(e.target.value)}
                    style={{ marginBottom: 'var(--space-2)', resize: 'none' }} />
                  <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                    <button className="btn btn-sm" style={{ flex: 1, background: 'var(--color-success-600)', color: '#fff', border: 'none', minWidth: 80 }}
                      onClick={() => handleDecision('agree')}>
                      Agree
                    </button>
                    {activeProject.categories
                      .filter(c => c.name !== selectedResult.ai_decision && c.name !== 'needs_review')
                      .map(c => (
                        <button key={c.name} className="btn btn-sm" style={{
                          flex: 1, background: CAT_COLORS[c.color] || 'var(--color-neutral-500)', color: '#fff', border: 'none', minWidth: 80,
                        }} onClick={() => handleDecision(`override_${c.name}`)}>
                          {c.name.replace(/_/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase())}
                        </button>
                      ))
                    }
                  </div>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {showSetup && <ReviewProjectSetup productionId={productionId} onCreated={handleProjectCreated} onCancel={() => setShowSetup(false)} />}
    </div>
  );
}
