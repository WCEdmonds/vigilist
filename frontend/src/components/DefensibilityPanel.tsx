import { useCallback, useEffect, useState } from 'react';
import {
  createSearchTermReport, drawSample, fetchExceptionsCsv, fetchSearchTermReportCsv,
  getChainOfCustody, getExceptionsReport, getSampleEstimate, listReviewProjects,
  listSamples, listSearchTermReports, listTarValidations, runSearchTermReport,
  runTarValidation,
  type SampleEstimate, type SampleInfo, type SearchTermReportInfo, type TarValidationInfo,
} from '../api/client';
import type { ReviewProject, Tag } from '../types';
import { showToast } from './Toast';

interface Props {
  productionId: number;
  tags: Tag[];
}

const pct = (x: number | null | undefined) =>
  x === null || x === undefined ? '—' : `${(x * 100).toFixed(1)}%`;

const sectionTitle = { fontSize: 'var(--text-sm)', fontWeight: 600, margin: '0 0 var(--space-2) 0' } as const;
const small = { fontSize: 'var(--text-xs)', color: 'var(--color-neutral-600)' } as const;

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export default function DefensibilityPanel({ productionId, tags }: Props) {
  const [open, setOpen] = useState(false);
  const [reports, setReports] = useState<SearchTermReportInfo[]>([]);
  const [samples, setSamples] = useState<SampleInfo[]>([]);
  const [validations, setValidations] = useState<TarValidationInfo[]>([]);
  const [projects, setProjects] = useState<ReviewProject[]>([]);
  const [custody, setCustody] = useState<Record<string, unknown> | null>(null);
  const [exceptionsTotal, setExceptionsTotal] = useState<number | null>(null);

  // search-term form
  const [strName, setStrName] = useState('');
  const [strTerms, setStrTerms] = useState('');
  const [openReportId, setOpenReportId] = useState<number | null>(null);

  // sampling form
  const [smpName, setSmpName] = useState('');
  const [smpPurpose, setSmpPurpose] = useState('richness');
  const [smpSize, setSmpSize] = useState('');
  const [smpProjectId, setSmpProjectId] = useState<number | ''>('');
  const [estimates, setEstimates] = useState<Record<number, SampleEstimate>>({});
  const [estimateTag, setEstimateTag] = useState<number | ''>('');

  // validation form
  const [valProjectId, setValProjectId] = useState<number | ''>('');
  const [valControlId, setValControlId] = useState<number | ''>('');
  const [valRespTag, setValRespTag] = useState<number | ''>('');
  const [valNonRespTag, setValNonRespTag] = useState<number | ''>('');
  const [valElusionId, setValElusionId] = useState<number | ''>('');

  const refresh = useCallback(() => {
    listSearchTermReports(productionId).then(setReports).catch(() => {});
    listSamples(productionId).then(setSamples).catch(() => {});
    listTarValidations(productionId).then(setValidations).catch(() => {});
    listReviewProjects(productionId).then(setProjects).catch(() => {});
    getChainOfCustody(productionId).then(setCustody).catch(() => {});
    getExceptionsReport(productionId).then(r => setExceptionsTotal(r.total)).catch(() => {});
  }, [productionId]);

  useEffect(() => { if (open) refresh(); }, [open, refresh]);

  const act = async (fn: () => Promise<unknown>, okMsg: string) => {
    try {
      await fn();
      showToast(okMsg, 'success');
      refresh();
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Failed', 'error');
    }
  };

  const latestValidation = validations.length ? validations[validations.length - 1] : null;
  const controlSamples = samples.filter(s => s.purpose === 'control');
  const elusionSamples = samples.filter(s => s.purpose === 'elusion');

  return (
    <div className="card" style={{ marginBottom: 'var(--space-4)', padding: 'var(--space-4)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 className="section-title" style={{ margin: 0 }}>Defensibility</h2>
        <button className="btn btn-ghost btn-sm" onClick={() => setOpen(o => !o)}>
          {open ? 'Hide' : 'Show'}
        </button>
      </div>
      {open && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)', marginTop: 'var(--space-3)' }}>

          {/* ── Search-term hit reports ── */}
          <div>
            <h3 style={sectionTitle}>Search-term hit reports</h3>
            <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <label><span style={small}>Report name</span>
                <input className="input input-sm" value={strName} onChange={e => setStrName(e.target.value)} placeholder="Negotiated terms v1" />
              </label>
              <label style={{ flex: 1, minWidth: 220 }}><span style={small}>Terms (one per line; phrases in quotes)</span>
                <textarea className="input" rows={2} value={strTerms} onChange={e => setStrTerms(e.target.value)} />
              </label>
              <button className="btn btn-secondary btn-sm"
                disabled={!strName.trim() || !strTerms.trim()}
                onClick={() => act(async () => {
                  await createSearchTermReport(productionId, strName,
                    strTerms.split('\n').map(t => t.trim()).filter(Boolean));
                  setStrName(''); setStrTerms('');
                }, 'Report created')}>
                Create
              </button>
            </div>
            {reports.map(r => (
              <div key={r.id} style={{ marginTop: 'var(--space-2)', borderTop: '1px solid rgba(44,62,107,0.08)', paddingTop: 'var(--space-2)' }}>
                <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
                  <strong style={{ fontSize: 'var(--text-sm)' }}>{r.name}</strong>
                  <span style={small}>{r.terms.length} terms{r.computed_at ? ` · run ${r.computed_at.slice(0, 10)}` : ' · never run'}</span>
                  <button className="btn btn-ghost btn-xs" onClick={() => act(() => runSearchTermReport(r.id), 'Report run')}>Run</button>
                  {r.results && (
                    <>
                      <button className="btn btn-ghost btn-xs" onClick={() => setOpenReportId(openReportId === r.id ? null : r.id)}>
                        {openReportId === r.id ? 'Hide' : 'Results'}
                      </button>
                      <button className="btn btn-ghost btn-xs"
                        onClick={() => fetchSearchTermReportCsv(r.id).then(b => saveBlob(b, `${r.name}_hits.csv`)).catch(() => showToast('CSV failed', 'error'))}>
                        CSV
                      </button>
                    </>
                  )}
                </div>
                {openReportId === r.id && r.results && (
                  <table style={{ fontSize: 'var(--text-xs)', marginTop: 4, borderCollapse: 'collapse' }}>
                    <thead><tr>
                      <th style={{ textAlign: 'left', paddingRight: 12 }}>Term</th>
                      <th style={{ paddingRight: 12 }}>Hits</th>
                      <th style={{ paddingRight: 12 }}>+Families</th>
                      <th>Unique</th>
                    </tr></thead>
                    <tbody>
                      {r.results.terms.map(t => (
                        <tr key={t.term}>
                          <td style={{ paddingRight: 12 }}>{t.term}</td>
                          <td style={{ textAlign: 'right', paddingRight: 12 }}>{t.hits}</td>
                          <td style={{ textAlign: 'right', paddingRight: 12 }}>{t.with_families}</td>
                          <td style={{ textAlign: 'right' }}>{t.unique_hits}</td>
                        </tr>
                      ))}
                      <tr style={{ fontWeight: 600 }}>
                        <td>ANY TERM (of {r.results.total_docs})</td>
                        <td style={{ textAlign: 'right', paddingRight: 12 }}>{r.results.any_hits}</td>
                        <td style={{ textAlign: 'right', paddingRight: 12 }}>{r.results.any_with_families}</td>
                        <td />
                      </tr>
                    </tbody>
                  </table>
                )}
              </div>
            ))}
          </div>

          {/* ── Sampling ── */}
          <div>
            <h3 style={sectionTitle}>Sampling</h3>
            <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <label><span style={small}>Name</span>
                <input className="input input-sm" value={smpName} onChange={e => setSmpName(e.target.value)} placeholder="Control set 1" />
              </label>
              <label><span style={small}>Purpose</span>
                <select className="input input-sm" value={smpPurpose} onChange={e => setSmpPurpose(e.target.value)}>
                  <option value="richness">richness</option>
                  <option value="acceptance">acceptance</option>
                  <option value="control">control</option>
                  <option value="elusion">elusion</option>
                </select>
              </label>
              <label><span style={small}>Size (blank = statistical)</span>
                <input className="input input-sm" type="number" min={1} value={smpSize} onChange={e => setSmpSize(e.target.value)} style={{ width: 110 }} />
              </label>
              {smpPurpose === 'elusion' && (
                <label><span style={small}>Review project (null set)</span>
                  <select className="input input-sm" value={smpProjectId} onChange={e => setSmpProjectId(e.target.value ? Number(e.target.value) : '')}>
                    <option value="">Choose…</option>
                    {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </label>
              )}
              <button className="btn btn-secondary btn-sm"
                disabled={!smpName.trim() || (smpPurpose === 'elusion' && smpProjectId === '')}
                onClick={() => act(async () => {
                  await drawSample(productionId, {
                    name: smpName, purpose: smpPurpose,
                    size: smpSize ? Number(smpSize) : null,
                    scope: smpPurpose === 'elusion' ? 'machine_negative' : null,
                    project_id: smpPurpose === 'elusion' && smpProjectId !== '' ? smpProjectId : null,
                  });
                  setSmpName(''); setSmpSize('');
                }, 'Sample drawn')}>
                Draw
              </button>
              <label><span style={small}>Estimate with tag</span>
                <select className="input input-sm" value={estimateTag} onChange={e => setEstimateTag(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">Choose tag…</option>
                  {tags.map(t => <option key={t.id} value={t.id}>{t.category}: {t.name}</option>)}
                </select>
              </label>
            </div>
            {samples.map(s => (
              <div key={s.id} style={{ ...small, marginTop: 4, display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
                <strong>{s.name}</strong>
                <span>{s.purpose} · {s.document_ids.length} docs · drawn {s.created_at.slice(0, 10)}</span>
                <button className="btn btn-ghost btn-xs" disabled={estimateTag === ''}
                  onClick={() => estimateTag !== '' && getSampleEstimate(s.id, estimateTag)
                    .then(e => setEstimates(prev => ({ ...prev, [s.id]: e })))
                    .catch(() => showToast('Estimate failed', 'error'))}>
                  Estimate
                </button>
                {estimates[s.id] && (
                  <span>
                    {estimates[s.id].positives}/{estimates[s.id].n} → {pct(estimates[s.id].rate)}
                    {' '}(CI {pct(estimates[s.id].ci_low)}–{pct(estimates[s.id].ci_high)};
                    {' '}≈{estimates[s.id].estimated_low}–{estimates[s.id].estimated_high} of {estimates[s.id].population})
                  </span>
                )}
              </div>
            ))}
          </div>

          {/* ── TAR validation ── */}
          <div>
            <h3 style={sectionTitle}>TAR validation</h3>
            <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <label><span style={small}>Project</span>
                <select className="input input-sm" value={valProjectId} onChange={e => setValProjectId(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">Choose…</option>
                  {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </label>
              <label><span style={small}>Control sample</span>
                <select className="input input-sm" value={valControlId} onChange={e => setValControlId(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">Choose…</option>
                  {controlSamples.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </label>
              <label><span style={small}>Responsive tag</span>
                <select className="input input-sm" value={valRespTag} onChange={e => setValRespTag(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">Choose…</option>
                  {tags.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
              </label>
              <label><span style={small}>Non-responsive tag (optional)</span>
                <select className="input input-sm" value={valNonRespTag} onChange={e => setValNonRespTag(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">None</option>
                  {tags.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
              </label>
              <label><span style={small}>Elusion sample (optional)</span>
                <select className="input input-sm" value={valElusionId} onChange={e => setValElusionId(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">None</option>
                  {elusionSamples.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </label>
              <button className="btn btn-secondary btn-sm"
                disabled={valProjectId === '' || valControlId === '' || valRespTag === ''}
                onClick={() => act(() => runTarValidation(productionId, {
                  project_id: valProjectId as number,
                  control_sample_id: valControlId as number,
                  responsive_tag_id: valRespTag as number,
                  nonresponsive_tag_id: valNonRespTag === '' ? null : valNonRespTag,
                  elusion_sample_id: valElusionId === '' ? null : valElusionId,
                }), 'Validation run')}>
                Run validation
              </button>
            </div>
            {latestValidation && (
              <div style={{ ...small, marginTop: 'var(--space-2)' }}>
                <div style={{ fontSize: 'var(--text-sm)', color: 'inherit' }}>
                  <strong>Latest (report #{latestValidation.id}):</strong>
                  {' '}Recall {pct(latestValidation.results.control.recall?.rate)}
                  {latestValidation.results.control.recall && ` (${pct(latestValidation.results.control.recall.low)}–${pct(latestValidation.results.control.recall.high)})`}
                  {' '}· Precision {pct(latestValidation.results.control.precision?.rate)}
                  {latestValidation.results.elusion && (
                    <> · Elusion {pct(latestValidation.results.elusion.rate)} (≈{latestValidation.results.elusion.estimated_missed_low}–{latestValidation.results.elusion.estimated_missed_high} of {latestValidation.results.elusion.null_set_size} missed)</>
                  )}
                </div>
                <div>
                  Matrix: TP {latestValidation.results.control.confusion.tp} · FP {latestValidation.results.control.confusion.fp}
                  {' '}· FN {latestValidation.results.control.confusion.fn} · TN {latestValidation.results.control.confusion.tn}
                  {' '}({latestValidation.results.control.coded}/{latestValidation.results.control.n} coded)
                </div>
                {latestValidation.results.control.notes.map((n, i) => <div key={i}>⚠ {n}</div>)}
                <div>{validations.length} report{validations.length === 1 ? '' : 's'} on file.</div>
              </div>
            )}
          </div>

          {/* ── Custody & exceptions ── */}
          <div>
            <h3 style={sectionTitle}>Chain of custody & exceptions</h3>
            {custody && (
              <div style={small}>
                <div>
                  Loads: {(custody.loads as unknown[]).length}
                  {' '}· Documents: {(custody.documents as { total: number }).total}
                  {' '}({(custody.documents as { hashed_sha256: number }).hashed_sha256} SHA-256 hashed)
                  {' '}· Production sets: {(custody.productions as unknown[]).length}
                </div>
              </div>
            )}
            <div style={{ ...small, marginTop: 4, display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
              <span>
                {exceptionsTotal === null ? 'Exceptions: —'
                  : exceptionsTotal === 0 ? 'No processing exceptions.'
                  : `${exceptionsTotal} processing exception${exceptionsTotal === 1 ? '' : 's'}.`}
              </span>
              {exceptionsTotal !== null && exceptionsTotal > 0 && (
                <button className="btn btn-ghost btn-xs"
                  onClick={() => fetchExceptionsCsv(productionId).then(b => saveBlob(b, 'exceptions_report.csv')).catch(() => showToast('CSV failed', 'error'))}>
                  Exceptions CSV
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
