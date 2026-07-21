import { useEffect, useRef, useState } from 'react';
import { ref, uploadBytesResumable } from 'firebase/storage';
import { firebaseStorage, auth } from '../firebase';
import { analyzeLoadFile, createProductionForIngest, getClassifyEstimate, getIngestStatus, startAutoClassification, startProcessing } from '../api/client';
import type { ProposedColumn } from '../api/client';
import { showToast } from './Toast';
import type { ClassifyEstimate, IngestJob } from '../types';

interface Props {
  onClose: () => void;
  onComplete: () => void;
}

type Stage = 'setup' | 'uploading' | 'mapping' | 'processing' | 'complete' | 'error';

const CANONICAL_FIELDS = [
  'bates_begin', 'bates_end', 'page_count', 'text_link', 'native_link', 'custodian',
  'date_sent', 'date_received', 'date_created', 'date_modified', 'file_hash_md5',
  'file_hash_sha256', 'file_type', 'file_name', 'source_path', 'email_from', 'email_to',
  'email_cc', 'email_bcc', 'email_subject',
] as const;

export default function IngestWizard({ onClose, onComplete }: Props) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [caseContext, setCaseContext] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [mode, setMode] = useState<'relativity' | 'generic_pdf' | 'native'>('relativity');
  const [custodian, setCustodian] = useState('');
  const [modeWarning, setModeWarning] = useState('');
  const [stage, setStage] = useState<Stage>('setup');
  const [uploadProgress, setUploadProgress] = useState({ uploaded: 0, total: 0, bytesUploaded: 0, totalBytes: 0, startTime: 0 });
  const [job, setJob] = useState<IngestJob | null>(null);
  const [error, setError] = useState('');
  const [columns, setColumns] = useState<ProposedColumn[]>([]);
  const [mappingProdId, setMappingProdId] = useState<number | null>(null);
  const totalFilesRef = useRef<number>(0);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const [classifyEstimate, setClassifyEstimate] = useState<ClassifyEstimate | null>(null);
  const [classifyEstimateFailed, setClassifyEstimateFailed] = useState(false);
  const [shouldClassify, setShouldClassify] = useState(true);
  const [startingClassification, setStartingClassification] = useState(false);

  // Set webkitdirectory attribute via ref (React doesn't support it as a prop)
  useEffect(() => {
    if (folderInputRef.current) {
      folderInputRef.current.setAttribute('webkitdirectory', '');
      folderInputRef.current.setAttribute('directory', '');
    }
  }, []);

  const handleFolderSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const fileList = e.target.files;
    if (!fileList) return;
    const selected = Array.from(fileList);

    const hasDat = selected.some(f => {
      const path = f.webkitRelativePath.toUpperCase();
      return path.includes('/DATA/') && path.endsWith('.DAT');
    });
    const pdfCount = selected.filter(f => f.name.toLowerCase().endsWith('.pdf')).length;

    if (selected.length === 0) {
      setError('No files found in the selected folder.');
      setFiles([]);
      return;
    }

    // Auto-detect and pre-select the most likely mode
    const detected: 'relativity' | 'generic_pdf' | 'native' = hasDat
      ? 'relativity'
      : pdfCount > 0
      ? 'generic_pdf'
      : 'native';
    setMode(detected);
    setFiles(selected);
    setError('');
    setModeWarning(detected === 'native' ? 'All files will be processed as native documents.' : '');
  };

  const chooseMode = (next: 'relativity' | 'generic_pdf' | 'native') => {
    setMode(next);
    if (files.length === 0) {
      setModeWarning('');
      return;
    }
    const hasDat = files.some(f => {
      const path = f.webkitRelativePath.toUpperCase();
      return path.includes('/DATA/') && path.endsWith('.DAT');
    });
    const pdfCount = files.filter(f => f.name.toLowerCase().endsWith('.pdf')).length;
    if (next === 'relativity' && !hasDat) {
      setModeWarning('No DATA/*.dat file found in this folder — Relativity ingest will fail.');
    } else if (next === 'generic_pdf' && pdfCount === 0) {
      setModeWarning('No PDF files found in this folder.');
    } else if (next === 'native') {
      setModeWarning('All files will be processed as native documents.');
    } else {
      setModeWarning('');
    }
  };

  const handleStart = async () => {
    if (!name.trim() || files.length === 0) return;
    setError('');

    // In PDF mode, only upload the PDFs (skip everything else in the folder)
    const uploadList = mode === 'generic_pdf'
      ? files.filter(f => f.name.toLowerCase().endsWith('.pdf'))
      : files;

    if (uploadList.length === 0) {
      setError(mode === 'generic_pdf' ? 'No PDF files to upload.' : 'No files to upload.');
      return;
    }

    setStage('uploading');
    const totalBytes = uploadList.reduce((sum, f) => sum + f.size, 0);
    setUploadProgress({ uploaded: 0, total: uploadList.length, bytesUploaded: 0, totalBytes, startTime: Date.now() });

    try {
      // Phase 1: Create production in backend to get real production_id
      // This also syncs Firebase custom claims so we can write to Storage
      const { production_id } = await createProductionForIngest(name.trim(), description.trim(), caseContext.trim());

      // Refresh the Firebase token to pick up the new custom claims
      const currentUser = auth.currentUser;
      if (currentUser) {
        await currentUser.getIdToken(true); // force refresh
      }

      // Phase 2: Upload files to Firebase Storage under the real production path
      // Use resumable uploads for real-time progress and automatic retry on failure.
      // Shared mutable counters are safe here — JS is single-threaded so callbacks
      // from concurrent uploads won't interleave mid-increment.
      let filesCompleted = 0;
      let totalBytesUploaded = 0;

      const uploadFile = (file: File): Promise<void> =>
        new Promise((resolve, reject) => {
          const parts = file.webkitRelativePath.split('/');
          const relativePath = parts.slice(1).join('/');
          const storagePath = `productions/${production_id}/raw/${relativePath}`;
          const task = uploadBytesResumable(ref(firebaseStorage, storagePath), file);
          let fileBytesTransferred = 0;

          task.on(
            'state_changed',
            (snapshot) => {
              const delta = snapshot.bytesTransferred - fileBytesTransferred;
              fileBytesTransferred = snapshot.bytesTransferred;
              totalBytesUploaded += delta;
              setUploadProgress(prev => ({ ...prev, bytesUploaded: totalBytesUploaded }));
            },
            reject,
            () => {
              filesCompleted++;
              setUploadProgress(prev => ({ ...prev, uploaded: filesCompleted, bytesUploaded: totalBytesUploaded }));
              resolve();
            },
          );
        });

      const batchSize = 50;
      for (let i = 0; i < uploadList.length; i += batchSize) {
        await Promise.all(uploadList.slice(i, i + batchSize).map(uploadFile));
      }

      // Phase 3: For relativity mode, analyze the load file first (mapping stage).
      // For generic_pdf, go straight to processing — there is no DAT load file to map.
      totalFilesRef.current = uploadList.length;
      if (mode === 'relativity') {
        const analysis = await analyzeLoadFile(production_id);
        setMappingProdId(production_id);
        setColumns(analysis.columns);
        setStage('mapping');
      } else {
        setStage('processing');
        const ingestJob = await startProcessing(production_id, uploadList.length, mode, {}, custodian);
        setJob(ingestJob);
        pollStatus(ingestJob.id);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Upload failed');
      setStage('error');
    }
  };

  const handleConfirmMapping = async () => {
    if (mappingProdId === null) return;
    const fieldMapping = Object.fromEntries(
      columns.filter(c => c.target).map(c => [c.target!, c.source_name]),
    );
    setStage('processing');
    try {
      const ingestJob = await startProcessing(mappingProdId, totalFilesRef.current, 'relativity', fieldMapping);
      setJob(ingestJob);
      pollStatus(ingestJob.id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to start processing');
      setStage('error');
    }
  };

  const pollStatus = async (jobId: string) => {
    const poll = async () => {
      try {
        const status = await getIngestStatus(jobId);
        setJob(status);
        if (status.status === 'complete') {
          setStage('complete');
        } else if (status.status === 'failed') {
          setError('Processing failed: ' + (status.errors[status.errors.length - 1] || 'Unknown error'));
          setStage('error');
        } else {
          setTimeout(poll, 2000);
        }
      } catch {
        setTimeout(poll, 3000);
      }
    };
    poll();
  };

  // Fetch the classification cost estimate once the ingest reaches 'complete'.
  useEffect(() => {
    if (stage !== 'complete' || !job?.production_id) return;
    let cancelled = false;
    setClassifyEstimate(null);
    setClassifyEstimateFailed(false);
    setShouldClassify(true);
    getClassifyEstimate(job.production_id)
      .then(est => { if (!cancelled) setClassifyEstimate(est); })
      .catch(() => { if (!cancelled) setClassifyEstimateFailed(true); });
    return () => { cancelled = true; };
  }, [stage, job?.production_id]);

  const handleViewProduction = async () => {
    if (shouldClassify && classifyEstimate && job?.production_id) {
      setStartingClassification(true);
      try {
        await startAutoClassification(job.production_id);
        showToast('Classification started', 'success');
      } catch (e) {
        showToast(e instanceof Error ? e.message : 'Failed to start classification', 'error');
      } finally {
        setStartingClassification(false);
      }
    }
    onComplete();
    onClose();
  };

  const formatEta = (seconds: number) => {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return s > 0 ? `${m}m ${s}s` : `${m}m`;
  };

  const uploadSpeed = uploadProgress.startTime > 0 && uploadProgress.bytesUploaded > 0
    ? uploadProgress.bytesUploaded / ((Date.now() - uploadProgress.startTime) / 1000)
    : 0;
  const etaSeconds = uploadSpeed > 0 && uploadProgress.totalBytes > uploadProgress.bytesUploaded
    ? (uploadProgress.totalBytes - uploadProgress.bytesUploaded) / uploadSpeed
    : 0;
  const speedLabel = uploadSpeed > 0
    ? uploadSpeed >= 1_000_000
      ? `${(uploadSpeed / 1_000_000).toFixed(1)} MB/s`
      : `${(uploadSpeed / 1_000).toFixed(0)} KB/s`
    : '';

  const progressPercent = stage === 'uploading' && uploadProgress.totalBytes > 0
    ? Math.round((uploadProgress.bytesUploaded / uploadProgress.totalBytes) * 100)
    : job && job.total_files > 0
    ? Math.round(((job.processed_files + (job.skipped_files || 0)) / job.total_files) * 100)
    : 0;

  const isActive = stage === 'uploading' || stage === 'processing' || stage === 'mapping';
  const isDone = stage === 'complete' || stage === 'error';
  const [minimized, setMinimized] = useState(false);

  const handleClose = () => {
    if (isActive) { setMinimized(true); return; }
    onClose();
  };

  const fmt = (b: number) => b >= 1_000_000 ? `${(b / 1_000_000).toFixed(1)} MB` : `${(b / 1_000).toFixed(0)} KB`;

  const statusLine = stage === 'uploading'
    ? `${fmt(uploadProgress.bytesUploaded)} / ${fmt(uploadProgress.totalBytes)}${speedLabel ? ` · ${speedLabel}${etaSeconds > 1 ? ` · ${formatEta(etaSeconds)} remaining` : ''}` : ''}`
    : stage === 'mapping'
    ? `Review column mapping · ${columns.length} columns detected`
    : stage === 'processing'
    ? job ? `Processing · ${job.processed_files} ingested${job.skipped_files ? ` · ${job.skipped_files} skipped` : ''} · ${job.processed_files + (job.skipped_files || 0)} / ${job.total_files} total` : 'Processing…'
    : stage === 'complete'
    ? `Done · ${job?.processed_files ?? 0} documents`
    : error;

  // Corner progress panel (shown when minimized or when active and not minimized—renders behind modal too)
  const cornerPanel = (isActive || isDone) && minimized && (
    <div
      style={{
        position: 'fixed', bottom: 24, right: 24, zIndex: 1100,
        width: 320, background: 'var(--color-neutral-900)', color: '#fff',
        borderRadius: 'var(--radius-lg)', boxShadow: '0 8px 32px rgba(44,62,107,0.35)',
        overflow: 'hidden',
      }}
    >
      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: '10px 14px' }}>
        {isActive && <span className="spinner spinner-sm" style={{ flexShrink: 0 }} />}
        {stage === 'complete' && <span style={{ color: 'var(--color-success-400)', fontSize: 14 }}>✓</span>}
        {stage === 'error' && <span style={{ color: 'var(--color-danger-400)', fontSize: 14 }}>✕</span>}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {name}
          </div>
          <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)', marginTop: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {statusLine}
          </div>
        </div>
        <button
          onClick={() => setMinimized(false)}
          title="Expand"
          style={{ background: 'none', border: 'none', color: 'rgba(255,255,255,0.5)', cursor: 'pointer', fontSize: 14, padding: '0 2px', lineHeight: 1 }}
        >
          ↗
        </button>
        {isDone && (
          <button
            onClick={() => { if (stage === 'complete') onComplete(); onClose(); }}
            title="Dismiss"
            style={{ background: 'none', border: 'none', color: 'rgba(255,255,255,0.4)', cursor: 'pointer', fontSize: 16, padding: '0 2px', lineHeight: 1 }}
          >
            ×
          </button>
        )}
      </div>
      {/* Progress bar */}
      {(isActive || stage === 'complete') && (
        <div style={{ height: 3, background: 'rgba(255,255,255,0.1)' }}>
          <div style={{
            height: '100%',
            width: `${stage === 'complete' ? 100 : progressPercent}%`,
            background: stage === 'complete' ? 'var(--color-success-400)' : 'var(--color-brand-400)',
            transition: 'width 0.3s ease',
          }} />
        </div>
      )}
    </div>
  );

  if (minimized) return <>{cornerPanel}</>;

  return (
    <>
      {cornerPanel}
      <div className="modal-overlay" onClick={handleClose}>
        <div className="modal-panel" style={{ width: 520 }} onClick={e => e.stopPropagation()}>
          <div className="modal-header">
            <h3 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>
              Ingest Production
            </h3>
            <button className="btn btn-ghost btn-sm" onClick={handleClose}>
              {isActive ? 'Minimize' : 'Close'}
            </button>
          </div>

          <div style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
            {stage === 'setup' && (
              <>
                <div>
                  <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                    Production Name
                  </label>
                  <input className="input" value={name} onChange={e => setName(e.target.value)} placeholder="e.g., SCHLEGEL_PROD001" />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                    Description (optional)
                  </label>
                  <input className="input" value={description} onChange={e => setDescription(e.target.value)} placeholder="Brief description" />
                </div>
                <div>
                  <label className="input-label" htmlFor="ingest-case-context">
                    About this case <span className="brief-ai-mark">✦</span>
                  </label>
                  <p className="input-hint">
                    A few sentences: what the case is about and what makes a document
                    relevant. The AI uses this to brief your team and, later, to
                    classify documents. You can edit it anytime in Production settings.
                  </p>
                  <textarea
                    id="ingest-case-context"
                    className="input"
                    rows={4}
                    value={caseContext}
                    onChange={e => setCaseContext(e.target.value)}
                    placeholder="e.g. Product-liability suit over the March 2024 recall. Relevant: anything about the recall decision, board discussions, or customer injuries."
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                    Upload Type
                  </label>
                  <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                    <button
                      type="button"
                      className={mode === 'relativity' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
                      onClick={() => chooseMode('relativity')}
                    >
                      Relativity production
                    </button>
                    <button
                      type="button"
                      className={mode === 'generic_pdf' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
                      onClick={() => chooseMode('generic_pdf')}
                    >
                      Folder of files (PDFs)
                    </button>
                    <button
                      type="button"
                      className={mode === 'native' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
                      onClick={() => chooseMode('native')}
                    >
                      Native files
                    </button>
                  </div>
                  {mode === 'native' && (
                    <label style={{ display: 'block', marginTop: 8 }}>
                      <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>Custodian (optional)</span>
                      <input className="input" value={custodian} onChange={e => setCustodian(e.target.value)} placeholder="e.g. Jane Smith" maxLength={255} />
                    </label>
                  )}
                  {modeWarning && (
                    <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-warning-700, #92400e)' }}>
                      {modeWarning}
                    </div>
                  )}
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                    Production Folder
                  </label>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                    <button className="btn btn-secondary btn-sm" onClick={() => folderInputRef.current?.click()}>
                      Select Folder
                    </button>
                    <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>
                      {files.length > 0 ? `${files.length} files selected` : 'No folder selected'}
                    </span>
                    <input
                      ref={folderInputRef}
                      type="file"
                      onChange={handleFolderSelect}
                      style={{ display: 'none' }}
                      multiple
                    />
                  </div>
                </div>

                {error && (
                  <div style={{ padding: 'var(--space-2) var(--space-3)', fontSize: 'var(--text-sm)', color: 'var(--color-danger-700)', background: 'var(--color-danger-50)', border: '1px solid var(--color-danger-100)', borderRadius: 'var(--radius-md)' }}>
                    {error}
                  </div>
                )}

                <button
                  className="btn btn-primary"
                  onClick={handleStart}
                  disabled={!name.trim() || files.length === 0}
                  style={{ width: '100%' }}
                >
                  Start Ingest
                </button>
              </>
            )}

            {(stage === 'uploading' || stage === 'processing') && (
              <div style={{ textAlign: 'center', padding: 'var(--space-4)' }}>
                <div className="spinner spinner-md" style={{ margin: '0 auto var(--space-4)' }} />
                <div style={{ fontSize: 'var(--text-sm)', fontWeight: 'var(--font-medium)', marginBottom: 'var(--space-2)' }}>
                  {stage === 'uploading' ? 'Uploading files…' : 'Processing production…'}
                </div>
                <div style={{ width: '100%', height: 6, background: 'var(--color-neutral-200)', borderRadius: 3, overflow: 'hidden', marginBottom: 'var(--space-2)' }}>
                  <div style={{ width: `${progressPercent}%`, height: '100%', background: 'var(--color-brand-500)', borderRadius: 3, transition: 'width 0.3s ease' }} />
                </div>
                <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', fontFamily: 'var(--font-mono)' }}>
                  {statusLine}
                </div>
              </div>
            )}

            {stage === 'mapping' && (
              <div>
                <div style={{ fontSize: 'var(--text-sm)', fontWeight: 'var(--font-medium)', marginBottom: 'var(--space-3)' }}>
                  Review detected columns and confirm field mapping
                </div>
                <div style={{ overflowX: 'auto', maxHeight: 380, overflowY: 'auto', border: '1px solid var(--color-neutral-200)', borderRadius: 'var(--radius-md)' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--text-xs)' }}>
                    <thead>
                      <tr style={{ background: 'var(--color-neutral-50)', position: 'sticky', top: 0 }}>
                        <th style={{ padding: '8px 10px', textAlign: 'left', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em', whiteSpace: 'nowrap', borderBottom: '1px solid var(--color-neutral-200)' }}>Source Column</th>
                        <th style={{ padding: '8px 10px', textAlign: 'left', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: '1px solid var(--color-neutral-200)' }}>Samples</th>
                        <th style={{ padding: '8px 10px', textAlign: 'left', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em', whiteSpace: 'nowrap', borderBottom: '1px solid var(--color-neutral-200)' }}>Map To</th>
                        <th style={{ padding: '8px 10px', textAlign: 'left', fontWeight: 'var(--font-semibold)', color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: '1px solid var(--color-neutral-200)' }}>Source</th>
                      </tr>
                    </thead>
                    <tbody>
                      {columns.map((col, i) => {
                        const badgeStyle: React.CSSProperties =
                          col.source === 'alias'
                            ? { background: 'var(--color-success-100)', color: 'var(--color-success-700)', padding: '2px 6px', borderRadius: 4, fontSize: 11, fontWeight: 600 }
                            : col.source === 'ai'
                            ? { background: '#fef3c7', color: '#92400e', padding: '2px 6px', borderRadius: 4, fontSize: 11, fontWeight: 600 }
                            : { background: 'var(--color-neutral-100)', color: 'var(--color-neutral-500)', padding: '2px 6px', borderRadius: 4, fontSize: 11, fontWeight: 600 };
                        return (
                          <tr key={`${col.source_name}-${i}`} style={{ borderBottom: '1px solid var(--color-neutral-100)' }}>
                            <td style={{ padding: '7px 10px', fontFamily: 'var(--font-mono)', whiteSpace: 'nowrap' }}>{col.source_name}</td>
                            <td style={{ padding: '7px 10px', color: 'var(--color-neutral-500)', maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={col.samples.join(', ')}>{col.samples.join(', ')}</td>
                            <td style={{ padding: '7px 10px' }}>
                              <select
                                value={col.target ?? ''}
                                onChange={e => {
                                  const next = e.target.value || null;
                                  setColumns(prev => prev.map((c, idx) => idx === i ? { ...c, target: next } : c));
                                }}
                                style={{ fontSize: 'var(--text-xs)', padding: '3px 6px', borderRadius: 'var(--radius-sm)', border: '1px solid var(--color-neutral-300)', background: 'var(--color-neutral-0, #fff)', maxWidth: 180 }}
                              >
                                <option value="">— leave in metadata —</option>
                                {CANONICAL_FIELDS.map(f => (
                                  <option key={f} value={f}>{f}</option>
                                ))}
                              </select>
                            </td>
                            <td style={{ padding: '7px 10px' }}>
                              <span style={badgeStyle}>{col.source}</span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <div style={{ marginTop: 'var(--space-4)', display: 'flex', justifyContent: 'flex-end' }}>
                  <button className="btn btn-primary" onClick={handleConfirmMapping}>
                    Start Processing
                  </button>
                </div>
              </div>
            )}

            {stage === 'complete' && (
              <div style={{ textAlign: 'center', padding: 'var(--space-4)' }}>
                <div style={{ fontSize: 'var(--text-lg)', fontFamily: 'var(--font-serif)', color: 'var(--color-success-700)', marginBottom: 'var(--space-2)' }}>
                  Ingest Complete
                </div>
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-4)' }}>
                  {job?.processed_files} documents ingested
                  {job?.skipped_files ? ` · ${job.skipped_files} skipped` : ''}
                  {job?.errors && job.errors.length > 0 && ` · ${job.errors.length} warnings`}
                </div>
                {caseContext.trim() !== '' && !classifyEstimateFailed && !classifyEstimate && (
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: 'var(--space-2)', justifyContent: 'center',
                    fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-4)',
                  }}>
                    <span className="spinner spinner-sm" />
                    Estimating cost…
                  </div>
                )}
                {caseContext.trim() !== '' && !classifyEstimateFailed && classifyEstimate && (
                  <label
                    style={{
                      display: 'flex', alignItems: 'flex-start', gap: 'var(--space-2)',
                      textAlign: 'left', marginBottom: 'var(--space-4)', padding: 'var(--space-2) var(--space-3)',
                      background: 'var(--color-brass-soft)', borderRadius: 'var(--radius-md)', cursor: 'pointer',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={shouldClassify}
                      onChange={e => setShouldClassify(e.target.checked)}
                      style={{ marginTop: 3, cursor: 'pointer' }}
                    />
                    <span style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-700)' }}>
                      <span className="brief-ai-mark">✦</span> Classify all {classifyEstimate.doc_count} documents
                      against your case description — est. ${classifyEstimate.est_usd.toFixed(2)}
                    </span>
                  </label>
                )}
                <button
                  className="btn btn-primary"
                  onClick={handleViewProduction}
                  disabled={startingClassification}
                >
                  {startingClassification ? 'Starting classification…' : 'View Production'}
                </button>
              </div>
            )}

            {stage === 'error' && (
              <div style={{ textAlign: 'center', padding: 'var(--space-4)' }}>
                <div style={{ fontSize: 'var(--text-lg)', color: 'var(--color-danger-700)', marginBottom: 'var(--space-2)' }}>
                  Ingest Failed
                </div>
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-danger-600)', marginBottom: 'var(--space-4)' }}>
                  {error}
                </div>
                <button className="btn btn-secondary" onClick={() => { setStage('setup'); setError(''); }}>
                  Try Again
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
