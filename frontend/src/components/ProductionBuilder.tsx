import { useCallback, useEffect, useState } from 'react';
import {
  addProductionSetDocuments, createProductionSet, deleteProductionSet, fetchProducedPdf,
  fetchProductionPackage, getProductionSet, getProductionSetManifest, getProductionSetMembers,
  getProductionSetValidation, lockProductionSet, packageProductionSet, renderProductionSet,
  type ManifestReport, type ProductionSetInfo, type ProductionSetMember,
  type ValidationConflict, type ValidationReport,
} from '../api/client';
import type { Tag } from '../types';

interface Props {
  productionId: number;
  setId: number | 'new';
  tags: Tag[];
  selectedIds: Set<string>;
  existingSets: ProductionSetInfo[];
  onOpenDoc: (id: string) => void;
  onClose: () => void;
}

const CONFLICT_LABELS: Record<string, string> = {
  qc_pending: 'Redaction QC not approved',
  privilege_produce: 'Privileged document would be produced unredacted',
  no_images: 'No page images',
  received_document: 'Received from another party',
};

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const fieldLabel = { fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' } as const;

export default function ProductionBuilder({ productionId, setId, tags, selectedIds, existingSets, onOpenDoc, onClose }: Props) {
  const [set, setSet] = useState<ProductionSetInfo | null>(null);
  const [members, setMembers] = useState<ProductionSetMember[]>([]);
  const [validation, setValidation] = useState<ValidationReport | null>(null);
  const [manifest, setManifest] = useState<ManifestReport | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  // create form
  const [name, setName] = useState('');
  const [prefix, setPrefix] = useState('PROD');
  const [padding, setPadding] = useState(6);
  const [startNumber, setStartNumber] = useState(1);
  const [startHint, setStartHint] = useState('');
  const [sortKey, setSortKey] = useState('control_number');
  const [designation, setDesignation] = useState('');
  const [imageFormat, setImageFormat] = useState<'pdf' | 'tiff'>('pdf');
  const [nativeTypes, setNativeTypes] = useState<string[]>([]);
  const [volumeCap, setVolumeCap] = useState('');

  // draft controls
  const [addTagId, setAddTagId] = useState<number | ''>('');
  const [includeFamilies, setIncludeFamilies] = useState(true);
  const [excludeDuplicates, setExcludeDuplicates] = useState(true);
  const [excludeReceived, setExcludeReceived] = useState(true);
  const [lastAdd, setLastAdd] = useState('');
  const [overrideConflicts, setOverrideConflicts] = useState(false);

  const currentSetId = set?.id ?? (typeof setId === 'number' ? setId : null);

  // Continue-from-previous: suggest the next number after the last locked set
  // sharing this prefix, so volumes keep one continuous Bates sequence.
  useEffect(() => {
    const ends = existingSets
      .filter(s => s.status === 'locked' && s.prefix === prefix && s.bates_end)
      .map(s => parseInt(s.bates_end!.slice(prefix.length), 10))
      .filter(n => Number.isFinite(n) && n > 0);
    if (ends.length) {
      const next = Math.max(...ends) + 1;
      setStartNumber(next);
      setStartHint(`continues from ${prefix}${String(Math.max(...ends)).padStart(padding, '0')}`);
    } else {
      setStartHint('');
    }
  }, [prefix, padding, existingSets]);

  const refresh = useCallback(async (id: number) => {
    const s = await getProductionSet(id);
    setSet(s);
    getProductionSetMembers(id).then(setMembers).catch(() => {});
    if (s.status === 'draft') {
      getProductionSetValidation(id).then(setValidation).catch(() => {});
    } else {
      getProductionSetManifest(id).then(setManifest).catch(() => {});
    }
    return s;
  }, []);

  useEffect(() => {
    if (typeof setId === 'number') {
      refresh(setId).catch(e => setError(e instanceof Error ? e.message : String(e)));
    }
  }, [setId, refresh]);

  // Poll while a render or package job runs.
  useEffect(() => {
    const active = set && (set.render_status === 'rendering' || set.package_status === 'packaging');
    if (!active || !currentSetId) return;
    const t = window.setInterval(() => { refresh(currentSetId).catch(() => {}); }, 3000);
    return () => window.clearInterval(t);
  }, [set, currentSetId, refresh]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleCreate = () => run(async () => {
    const s = await createProductionSet(productionId, {
      name, prefix, padding, start_number: startNumber, sort_key: sortKey,
      designation: designation || null,
      image_format: imageFormat,
      native_file_types: nativeTypes,
      volume_max_mb: volumeCap ? Number(volumeCap) : null,
    });
    setSet(s);
    setValidation({ qc_pending: [], privilege_produce: [], no_images: [], received_document: [], total: 0 });
  });

  const handleAdd = (body: { document_ids?: string[]; tag_id?: number }) => run(async () => {
    if (!currentSetId) return;
    const r = await addProductionSetDocuments(currentSetId, {
      ...body,
      include_families: includeFamilies,
      exclude_duplicates: excludeDuplicates,
      exclude_received: excludeReceived,
    });
    setLastAdd(`Added ${r.added} (skipped ${r.skipped_existing} existing, ${r.skipped_duplicates} duplicates, ${r.skipped_received} received; +${r.families_added} family members)`);
    await refresh(currentSetId);
  });

  const handleLock = () => run(async () => {
    if (!currentSetId) return;
    await lockProductionSet(currentSetId, overrideConflicts);
    await refresh(currentSetId);
  });

  const handleDelete = () => run(async () => {
    if (!currentSetId) return;
    await deleteProductionSet(currentSetId);
    onClose();
  });

  const handleRender = () => run(async () => {
    if (!currentSetId) return;
    await renderProductionSet(currentSetId);
    await refresh(currentSetId);
  });

  const handlePackage = () => run(async () => {
    if (!currentSetId) return;
    await packageProductionSet(currentSetId);
    await refresh(currentSetId);
  });

  const handleDownloadPackage = () => run(async () => {
    if (!currentSetId || !set) return;
    saveBlob(await fetchProductionPackage(currentSetId), `${set.prefix}_production.zip`);
  });

  const handleSpotCheck = (m: ProductionSetMember) => run(async () => {
    if (!currentSetId) return;
    saveBlob(await fetchProducedPdf(currentSetId, m.document_id), `${m.bates_begin ?? m.control_number}.pdf`);
  });

  const conflictEntries: [string, ValidationConflict[]][] = validation
    ? (Object.entries(validation).filter(([, v]) => Array.isArray(v) && v.length > 0) as [string, ValidationConflict[]][])
    : [];
  const conflictTotal = validation?.total ?? 0;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel" style={{ width: 640, maxHeight: '85vh', overflowY: 'auto' }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3 style={{ margin: 0 }}>{set ? set.name : 'New production set'}</h3>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
        </div>

        {error && (
          <div style={{ color: '#b00020', fontSize: 'var(--text-sm)', margin: 'var(--space-2) 0' }}>{error}</div>
        )}

        {/* ── Create ── */}
        {!set && setId === 'new' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
            <label><span style={fieldLabel}>Name</span>
              <input className="input" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Volume 1" maxLength={255} />
            </label>
            <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
              <label style={{ flex: 2 }}><span style={fieldLabel}>Bates prefix</span>
                <input className="input" value={prefix} onChange={e => setPrefix(e.target.value.toUpperCase())} maxLength={50} />
              </label>
              <label style={{ flex: 1 }}><span style={fieldLabel}>Digits</span>
                <input className="input" type="number" min={1} max={12} value={padding} onChange={e => setPadding(Number(e.target.value))} />
              </label>
              <label style={{ flex: 1 }}><span style={fieldLabel}>Start at</span>
                <input className="input" type="number" min={1} value={startNumber} onChange={e => setStartNumber(Number(e.target.value))} />
              </label>
            </div>
            {startHint && <div style={{ ...fieldLabel, marginTop: -4 }}>{startHint}</div>}
            <div style={{ ...fieldLabel }}>Preview: {prefix}{String(startNumber).padStart(padding, '0')}</div>
            <label><span style={fieldLabel}>Sort order</span>
              <select className="input" value={sortKey} onChange={e => setSortKey(e.target.value)}>
                <option value="control_number">Control number (load order)</option>
                <option value="custodian_date">Custodian, then date</option>
              </select>
            </label>
            <label><span style={fieldLabel}>Confidentiality designation (stamped on every page; optional)</span>
              <input className="input" value={designation} onChange={e => setDesignation(e.target.value)} placeholder="e.g. CONFIDENTIAL" maxLength={100} />
            </label>
            <div>
              <span style={fieldLabel}>Image format</span>
              <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                <button type="button"
                  className={imageFormat === 'pdf' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
                  onClick={() => setImageFormat('pdf')}>
                  PDF per document
                </button>
                <button type="button"
                  className={imageFormat === 'tiff' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
                  onClick={() => setImageFormat('tiff')}>
                  Single-page TIFF (classic)
                </button>
              </div>
            </div>
            <div>
              <span style={fieldLabel}>Produce natively (instead of images; produce-disposition docs only)</span>
              <div style={{ display: 'flex', gap: 'var(--space-3)', marginTop: 4, fontSize: 'var(--text-xs)', flexWrap: 'wrap' }}>
                {['spreadsheet', 'presentation', 'audio', 'video'].map(t => (
                  <label key={t}>
                    <input
                      type="checkbox"
                      checked={nativeTypes.includes(t)}
                      onChange={e => setNativeTypes(prev => e.target.checked ? [...prev, t] : prev.filter(x => x !== t))}
                    />
                    {' '}{t}
                  </label>
                ))}
              </div>
            </div>
            <label style={{ maxWidth: 260 }}><span style={fieldLabel}>Max volume size (MB, optional)</span>
              <input className="input" type="number" min={50} value={volumeCap}
                onChange={e => setVolumeCap(e.target.value)} placeholder="single volume" />
            </label>
            <button className="btn btn-primary" disabled={busy || !name.trim() || !prefix.trim()} onClick={handleCreate}>
              Create draft
            </button>
          </div>
        )}

        {/* ── Draft: build + validate ── */}
        {set && set.status === 'draft' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
            <div style={{ fontSize: 'var(--text-sm)' }}>
              <strong>{members.length}</strong> document{members.length === 1 ? '' : 's'} in this set
              {lastAdd && <div style={fieldLabel}>{lastAdd}</div>}
            </div>

            <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <label style={{ minWidth: 180 }}><span style={fieldLabel}>Add by tag</span>
                <select className="input input-sm" value={addTagId} onChange={e => setAddTagId(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">Choose a tag…</option>
                  {tags.map(t => <option key={t.id} value={t.id}>{t.category}: {t.name}</option>)}
                </select>
              </label>
              <button className="btn btn-secondary btn-sm" disabled={busy || addTagId === ''}
                onClick={() => addTagId !== '' && handleAdd({ tag_id: addTagId })}>
                Add tagged
              </button>
              <button className="btn btn-secondary btn-sm" disabled={busy || selectedIds.size === 0}
                onClick={() => handleAdd({ document_ids: Array.from(selectedIds) })}>
                Add {selectedIds.size} selected
              </button>
            </div>
            <div style={{ display: 'flex', gap: 'var(--space-4)', fontSize: 'var(--text-xs)' }}>
              <label><input type="checkbox" checked={includeFamilies} onChange={e => setIncludeFamilies(e.target.checked)} /> Include families</label>
              <label><input type="checkbox" checked={excludeDuplicates} onChange={e => setExcludeDuplicates(e.target.checked)} /> Exclude duplicates</label>
              <label><input type="checkbox" checked={excludeReceived} onChange={e => setExcludeReceived(e.target.checked)} /> Exclude received documents</label>
            </div>

            <div style={{ borderTop: '1px solid rgba(20, 24, 29,0.1)', paddingTop: 'var(--space-3)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <strong style={{ fontSize: 'var(--text-sm)' }}>Validation</strong>
                <button className="btn btn-ghost btn-xs" disabled={busy || !currentSetId}
                  onClick={() => currentSetId && run(() => getProductionSetValidation(currentSetId).then(setValidation))}>
                  Re-check
                </button>
              </div>
              {conflictTotal === 0 ? (
                <div style={{ color: '#1a7f37', fontSize: 'var(--text-sm)' }}>No conflicts — ready to lock.</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
                  {conflictEntries.map(([category, items]) => (
                    <div key={category}>
                      <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: '#b00020' }}>
                        {CONFLICT_LABELS[category] ?? category} ({items.length})
                      </div>
                      {items.slice(0, 25).map(c => (
                        <div key={`${category}-${c.document_id}`} style={{ fontSize: 'var(--text-xs)', display: 'flex', gap: 6 }}>
                          <button className="btn btn-ghost btn-xs" style={{ padding: '0 4px' }} onClick={() => onOpenDoc(c.document_id)}>
                            {c.control_number}
                          </button>
                          <span style={{ color: 'var(--color-neutral-600)' }}>{c.detail}</span>
                        </div>
                      ))}
                      {items.length > 25 && <div style={fieldLabel}>…and {items.length - 25} more</div>}
                    </div>
                  ))}
                  <label style={{ fontSize: 'var(--text-xs)' }}>
                    <input type="checkbox" checked={overrideConflicts} onChange={e => setOverrideConflicts(e.target.checked)} />
                    {' '}Override conflicts — this is recorded with my name
                  </label>
                </div>
              )}
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={handleDelete}>Delete draft</button>
              <button className="btn btn-primary" disabled={busy || members.length === 0 || (conflictTotal > 0 && !overrideConflicts)}
                onClick={handleLock}>
                Lock &amp; assign Bates
              </button>
            </div>
          </div>
        )}

        {/* ── Locked: render / package ── */}
        {set && set.status === 'locked' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
            <div style={{ fontSize: 'var(--text-sm)' }}>
              <strong>{set.doc_count}</strong> docs · <strong>{set.page_count ?? '—'}</strong> pages · {set.bates_begin} – {set.bates_end}
              {set.designation && <> · {set.designation}</>}
              {' '}· {(set.image_format || 'pdf').toUpperCase()}
              {set.volume_max_mb ? <> · volumes ≤ {set.volume_max_mb} MB</> : null}
              {set.native_file_types && set.native_file_types.length > 0 && (
                <> · native: {set.native_file_types.join(', ')}</>
              )}
              {set.conflicts_overridden_by && (
                <div style={{ ...fieldLabel, color: '#9a6700' }}>Conflicts overridden by {set.conflicts_overridden_by}</div>
              )}
            </div>

            {manifest && (
              <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)' }}>
                {Object.entries(manifest.counts).map(([k, v]) => `${k}: ${v}`).join(' · ')}
                <div style={{ color: manifest.continuity.ok ? '#1a7f37' : '#b00020' }}>
                  {manifest.continuity.ok ? 'Bates continuity verified — no gaps or overlaps.' : `Continuity errors: ${manifest.continuity.errors.join('; ')}`}
                </div>
              </div>
            )}

            {set.render_status === 'not_started' && (
              <button className="btn btn-primary" disabled={busy} onClick={handleRender}>
                Render production (burn redactions + stamp Bates)
              </button>
            )}
            {set.render_status === 'rendering' && (
              <div style={{ fontSize: 'var(--text-sm)' }}>
                Rendering… {set.rendered_count}/{set.doc_count}
                <div style={{ height: 6, background: 'rgba(20, 24, 29,0.1)', borderRadius: 3, marginTop: 4 }}>
                  <div style={{ height: 6, width: `${set.doc_count ? Math.round((set.rendered_count / set.doc_count) * 100) : 0}%`, background: 'var(--color-primary, #14181d)', borderRadius: 3 }} />
                </div>
              </div>
            )}
            {set.render_status === 'error' && (
              <div style={{ fontSize: 'var(--text-sm)' }}>
                <div style={{ color: '#b00020' }}>Render failed: {set.render_error}</div>
                <button className="btn btn-secondary btn-sm" disabled={busy} onClick={handleRender}>Re-render</button>
              </div>
            )}
            {set.render_status === 'rendered' && (
              <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
                {set.package_status === 'not_started' && (
                  <button className="btn btn-primary" disabled={busy} onClick={handlePackage}>
                    Package (DAT/OPT + PDFs + manifest ZIP)
                  </button>
                )}
                {set.package_status === 'packaging' && <span style={{ fontSize: 'var(--text-sm)' }}>Packaging…</span>}
                {set.package_status === 'error' && (
                  <>
                    <span style={{ color: '#b00020', fontSize: 'var(--text-sm)' }}>Packaging failed: {set.package_error}</span>
                    <button className="btn btn-secondary btn-sm" disabled={busy} onClick={handlePackage}>Retry</button>
                  </>
                )}
                {set.package_status === 'packaged' && (
                  <>
                    <button className="btn btn-primary" disabled={busy} onClick={handleDownloadPackage}>Download package</button>
                    <button className="btn btn-ghost btn-sm" disabled={busy} onClick={handlePackage}>Re-package</button>
                  </>
                )}
              </div>
            )}

            {members.length > 0 && (
              <div style={{ borderTop: '1px solid rgba(20, 24, 29,0.1)', paddingTop: 'var(--space-2)' }}>
                <div style={{ ...fieldLabel, marginBottom: 4 }}>Members {members.length > 100 ? '(first 100)' : ''}</div>
                <div style={{ maxHeight: 220, overflowY: 'auto', fontSize: 'var(--text-xs)' }}>
                  {members.slice(0, 100).map(m => (
                    <div key={m.document_id} style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', padding: '2px 0' }}>
                      <span style={{ minWidth: 110, fontWeight: 600 }}>{m.bates_begin}</span>
                      <span style={{ flex: 1, color: 'var(--color-neutral-600)' }}>{m.control_number}</span>
                      <span style={{ minWidth: 90 }}>{m.disposition}</span>
                      <span style={{ minWidth: 40 }}>{m.pages} pg</span>
                      {set.render_status === 'rendered' && (
                        <button className="btn btn-ghost btn-xs" disabled={busy} onClick={() => handleSpotCheck(m)}>PDF</button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {!set && setId !== 'new' && !error && (
          <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)' }}>Loading…</div>
        )}
      </div>
    </div>
  );
}
