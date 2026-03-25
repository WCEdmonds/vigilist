import { useEffect, useRef, useState } from 'react';
import { ref, uploadBytes } from 'firebase/storage';
import { firebaseStorage } from '../firebase';
import { startIngest, getIngestStatus } from '../api/client';
import type { IngestJob } from '../types';

interface Props {
  onClose: () => void;
  onComplete: () => void;
}

type Stage = 'setup' | 'uploading' | 'processing' | 'complete' | 'error';

export default function IngestWizard({ onClose, onComplete }: Props) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [stage, setStage] = useState<Stage>('setup');
  const [uploadProgress, setUploadProgress] = useState({ uploaded: 0, total: 0 });
  const [job, setJob] = useState<IngestJob | null>(null);
  const [error, setError] = useState('');
  const folderInputRef = useRef<HTMLInputElement>(null);

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

    // Validate: check for DATA/ directory with a .dat file
    const hasDat = selected.some(f => {
      const path = f.webkitRelativePath.toUpperCase();
      return path.includes('/DATA/') && path.endsWith('.DAT');
    });

    if (!hasDat) {
      setError('Selected folder must contain a DATA/ directory with a .dat file');
      return;
    }

    setFiles(selected);
    setError('');
  };

  const handleStart = async () => {
    if (!name.trim() || files.length === 0) return;
    setError('');
    setStage('uploading');
    setUploadProgress({ uploaded: 0, total: files.length });

    try {
      // Use a temporary production ID based on name for the storage path
      // The real production ID will be assigned by the backend
      const tempId = name.trim().replace(/[^a-zA-Z0-9_-]/g, '_');

      // Upload files in batches of 10
      const batchSize = 10;
      let uploaded = 0;

      for (let i = 0; i < files.length; i += batchSize) {
        const batch = files.slice(i, i + batchSize);
        await Promise.all(
          batch.map(async (file) => {
            // Get the relative path within the selected folder
            const parts = file.webkitRelativePath.split('/');
            // Remove the root folder name, keep the rest
            const relativePath = parts.slice(1).join('/');
            const storagePath = `productions/${tempId}/raw/${relativePath}`;
            const storageRef = ref(firebaseStorage, storagePath);
            await uploadBytes(storageRef, file);
          })
        );
        uploaded += batch.length;
        setUploadProgress({ uploaded, total: files.length });
      }

      // Start backend processing
      setStage('processing');
      const ingestJob = await startIngest(name.trim(), description.trim(), files.length);
      setJob(ingestJob);

      // Poll for status
      pollStatus(ingestJob.id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Upload failed');
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

  const progressPercent = stage === 'uploading' && uploadProgress.total > 0
    ? Math.round((uploadProgress.uploaded / uploadProgress.total) * 100)
    : job && job.total_files > 0
    ? Math.round((job.processed_files / job.total_files) * 100)
    : 0;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel" style={{ width: 520 }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>
            Ingest Production
          </h3>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
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
                {stage === 'uploading' ? 'Uploading files...' : 'Processing production...'}
              </div>
              <div style={{ width: '100%', height: 6, background: 'var(--color-neutral-200)', borderRadius: 3, overflow: 'hidden', marginBottom: 'var(--space-2)' }}>
                <div style={{ width: `${progressPercent}%`, height: '100%', background: 'var(--color-brand-500)', borderRadius: 3, transition: 'width 0.3s ease' }} />
              </div>
              <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', fontFamily: 'var(--font-mono)' }}>
                {stage === 'uploading'
                  ? `${uploadProgress.uploaded} / ${uploadProgress.total} files`
                  : job
                  ? `${job.processed_files} / ${job.total_files} documents`
                  : ''}
              </div>
            </div>
          )}

          {stage === 'complete' && (
            <div style={{ textAlign: 'center', padding: 'var(--space-4)' }}>
              <div style={{ fontSize: 'var(--text-lg)', fontFamily: 'var(--font-serif)', color: 'var(--color-success-700)', marginBottom: 'var(--space-2)' }}>
                Ingest Complete
              </div>
              <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-4)' }}>
                {job?.processed_files} documents processed
                {job?.errors && job.errors.length > 0 && ` (${job.errors.length} warnings)`}
              </div>
              <button className="btn btn-primary" onClick={() => { onComplete(); onClose(); }}>
                View Production
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
  );
}
