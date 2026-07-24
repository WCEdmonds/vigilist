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

const PURPOSES: { value: string; label: string; hint: string }[] = [
  { value: 'richness', label: 'Richness — how much responsive material is out there', hint: 'Estimate prevalence across the whole matter.' },
  { value: 'control', label: 'Control set — blind ground truth for validating AI review', hint: 'Reviewed by humans without seeing AI calls.' },
  { value: 'elusion', label: 'Elusion — check what the AI discarded', hint: 'Samples the machine-negative pile for misses.' },
  { value: 'acceptance', label: 'Acceptance — QC a finished batch', hint: 'Pass/fail a completed slice of review.' },
];

type DefTab = 'hits' | 'sampling' | 'validation' | 'custody';

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
  const [tab, setTab] = useState<DefTab>('hits');
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
  const purposeLabel = (p: string) => PURPOSES.find(x => x.value === p)?.label.split(' — ')[0] ?? p;

  if (!open) {
    return (
      <div className="def-strip">
        <span className="bates-chip">DEFENSIBILITY</span>
        <span className="def-strip-text">Hit reports, statistical sampling, AI validation, and chain of custody — the record behind this matter.</span>
        <button className="btn btn-ghost btn-sm" style={{ marginLeft: 'auto' }} onClick={() => setOpen(true)}>Open</button>
      </div>
    );
  }

  return (
    <div className="card" style={{ marginTop: 'var(--space-4)', padding: 'var(--space-4)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--space-2)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
          <h2 className="section-title" style={{ margin: 0 }}>Defensibility</h2>
          <span className="bates-chip">ON&nbsp;THE&nbsp;RECORD</span>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>Hide</button>
      </div>

      <div className="tabs" style={{ margin: '0 calc(-1 * var(--space-4)) var(--space-4)', padding: '0 var(--space-4)' }}>
        <button className={`tab ${tab === 'hits' ? 'active' : ''}`} onClick={() => setTab('hits')}>Hit reports</button>
        <button className={`tab ${tab === 'sampling' ? 'active' : ''}`} onClick={() => setTab('sampling')}>Sampling</button>
        <button className={`tab ${tab === 'validation' ? 'active' : ''}`} onClick={() => setTab('validation')}>AI validation</button>
        <button className={`tab ${tab === 'custody' ? 'active' : ''}`} onClick={() => setTab('custody')}>Custody</button>
      </div>

      {tab === 'hits' && (
        <div>
          <p className="def-explain">
            When you negotiate search terms with the other side, a hit report proves what each term
            actually finds — including <strong>unique hits</strong>: documents no other term catches,
            the number that decides whether a term is worth fighting over.
          </p>
          <div className="def-form">
            <label>
              <span className="input-label">Report name</span>
              <input className="input input-sm" value={strName} onChange={e => setStrName(e.target.value)} placeholder="Negotiated terms v1" />
            </label>
            <label style={{ flex: 1, minWidth: 240 }}>
              <span className="input-label">Search terms</span>
              <textarea className="input" rows={2} value={strTerms} onChange={e => setStrTerms(e.target.value)} placeholder={'wire transfer\n"payment approval"'} />
              <span className="input-hint">One per line. Quote exact phrases.</span>
            </label>
            <button className="btn btn-secondary btn-sm"
              disabled={!strName.trim() || !strTerms.trim()}
              onClick={() => act(async () => {
                await createSearchTermReport(productionId, strName,
                  strTerms.split('\n').map(t => t.trim()).filter(Boolean));
                setStrName(''); setStrTerms('');
              }, 'Report created')}>
              Create report
            </button>
          </div>
          {reports.map(r => (
            <div key={r.id} className="def-row">
              <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
                <strong style={{ fontSize: 'var(--text-sm)' }}>{r.name}</strong>
                <span className="def-meta">{r.terms.length} terms{r.computed_at ? ` · run ${r.computed_at.slice(0, 10)}` : ' · never run'}</span>
                <button className="btn btn-ghost btn-xs" onClick={() => act(() => runSearchTermReport(r.id), 'Report run')}>Run</button>
                {r.results && (
                  <>
                    <button className="btn btn-ghost btn-xs" onClick={() => setOpenReportId(openReportId === r.id ? null : r.id)}>
                      {openReportId === r.id ? 'Hide results' : 'Results'}
                    </button>
                    <button className="btn btn-ghost btn-xs"
                      onClick={() => fetchSearchTermReportCsv(r.id).then(b => saveBlob(b, `${r.name}_hits.csv`)).catch(() => showToast('CSV failed', 'error'))}>
                      CSV
                    </button>
                  </>
                )}
              </div>
              {openReportId === r.id && r.results && (
                <table className="def-table">
                  <thead><tr>
                    <th>Term</th>
                    <th className="num">Documents hit</th>
                    <th className="num">With families</th>
                    <th className="num">Unique hits</th>
                  </tr></thead>
                  <tbody>
                    {r.results.terms.map(t => (
                      <tr key={t.term}>
                        <td>{t.term}</td>
                        <td className="num">{t.hits.toLocaleString()}</td>
                        <td className="num">{t.with_families.toLocaleString()}</td>
                        <td className="num">{t.unique_hits.toLocaleString()}</td>
                      </tr>
                    ))}
                    <tr className="def-table-total">
                      <td>Any term · of {r.results.total_docs.toLocaleString()} documents</td>
                      <td className="num">{r.results.any_hits.toLocaleString()}</td>
                      <td className="num">{r.results.any_with_families.toLocaleString()}</td>
                      <td />
                    </tr>
                  </tbody>
                </table>
              )}
            </div>
          ))}
        </div>
      )}

      {tab === 'sampling' && (
        <div>
          <p className="def-explain">
            Frozen random samples the other side can't argue with — the draw is recorded once and
            never changes. Use them to estimate how much responsive material exists, or as the
            blind ground truth that AI validation is built on.
          </p>
          <div className="def-form">
            <label>
              <span className="input-label">Sample name</span>
              <input className="input input-sm" value={smpName} onChange={e => setSmpName(e.target.value)} placeholder="Control set 1" />
            </label>
            <label style={{ minWidth: 320 }}>
              <span className="input-label">Purpose</span>
              <select className="input input-sm" value={smpPurpose} onChange={e => setSmpPurpose(e.target.value)}>
                {PURPOSES.map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
              <span className="input-hint">{PURPOSES.find(p => p.value === smpPurpose)?.hint}</span>
            </label>
            <label>
              <span className="input-label">Sample size</span>
              <input className="input input-sm" type="number" min={1} value={smpSize} onChange={e => setSmpSize(e.target.value)} style={{ width: 110 }} placeholder="auto" />
              <span className="input-hint">Blank = statistically sized (95% confidence)</span>
            </label>
            {smpPurpose === 'elusion' && (
              <label>
                <span className="input-label">Review project</span>
                <select className="input input-sm" value={smpProjectId} onChange={e => setSmpProjectId(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">Choose…</option>
                  {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
                <span className="input-hint">Whose discard pile to sample</span>
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
              Draw sample
            </button>
          </div>
          {samples.length > 0 && (
            <div className="def-form" style={{ marginTop: 'var(--space-3)' }}>
              <label>
                <span className="input-label">Estimate prevalence of tag</span>
                <select className="input input-sm" value={estimateTag} onChange={e => setEstimateTag(e.target.value ? Number(e.target.value) : '')}>
                  <option value="">Choose a tag…</option>
                  {tags.map(t => <option key={t.id} value={t.id}>{t.category}: {t.name}</option>)}
                </select>
                <span className="input-hint">Then press Estimate on any drawn sample below</span>
              </label>
            </div>
          )}
          {samples.map(s => (
            <div key={s.id} className="def-row">
              <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
                <strong style={{ fontSize: 'var(--text-sm)' }}>{s.name}</strong>
                <span className="stamp-badge stamp-badge--ink">{purposeLabel(s.purpose)}</span>
                <span className="def-meta">{s.document_ids.length} docs · drawn {s.created_at.slice(0, 10)}</span>
                <button className="btn btn-ghost btn-xs" disabled={estimateTag === ''}
                  onClick={() => estimateTag !== '' && getSampleEstimate(s.id, estimateTag)
                    .then(e => setEstimates(prev => ({ ...prev, [s.id]: e })))
                    .catch(() => showToast('Estimate failed', 'error'))}>
                  Estimate
                </button>
              </div>
              {estimates[s.id] && (
                <div className="readout-line">
                  {estimates[s.id].positives}/{estimates[s.id].n}&nbsp;TAGGED&nbsp;→&nbsp;<b>{pct(estimates[s.id].rate)}</b>
                  &nbsp;[CI&nbsp;{pct(estimates[s.id].ci_low)}–{pct(estimates[s.id].ci_high)}]
                  &nbsp;·&nbsp;≈{estimates[s.id].estimated_low.toLocaleString()}–{estimates[s.id].estimated_high.toLocaleString()}&nbsp;OF&nbsp;{estimates[s.id].population.toLocaleString()}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {tab === 'validation' && (
        <div>
          <p className="def-explain">
            The numbers that let AI review take the stand. A blind-reviewed control set yields
            <strong> recall</strong> (how much responsive material the AI found) and
            <strong> precision</strong> (how much of what it flagged is really responsive);
            an elusion sample checks the discard pile. Reports are preserved exactly as run.
          </p>
          <div className="def-form">
            <label>
              <span className="input-label">AI review project</span>
              <select className="input input-sm" value={valProjectId} onChange={e => setValProjectId(e.target.value ? Number(e.target.value) : '')}>
                <option value="">Choose…</option>
                {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              <span className="input-hint">The AI whose calls are being validated</span>
            </label>
            <label>
              <span className="input-label">Control sample</span>
              <select className="input input-sm" value={valControlId} onChange={e => setValControlId(e.target.value ? Number(e.target.value) : '')}>
                <option value="">Choose…</option>
                {controlSamples.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
              <span className="input-hint">Draw one under Sampling first</span>
            </label>
            <label>
              <span className="input-label">"Responsive" tag</span>
              <select className="input input-sm" value={valRespTag} onChange={e => setValRespTag(e.target.value ? Number(e.target.value) : '')}>
                <option value="">Choose…</option>
                {tags.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
              <span className="input-hint">How human reviewers marked responsive docs</span>
            </label>
            <label>
              <span className="input-label">"Not responsive" tag</span>
              <select className="input input-sm" value={valNonRespTag} onChange={e => setValNonRespTag(e.target.value ? Number(e.target.value) : '')}>
                <option value="">Optional</option>
                {tags.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </label>
            <label>
              <span className="input-label">Elusion sample</span>
              <select className="input input-sm" value={valElusionId} onChange={e => setValElusionId(e.target.value ? Number(e.target.value) : '')}>
                <option value="">Optional</option>
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
            <div className="def-row">
              <div className="def-meta" style={{ marginBottom: 4 }}>Latest — report #{latestValidation.id} · {validations.length} report{validations.length === 1 ? '' : 's'} on file, preserved as run</div>
              <div className="readout-line">
                RECALL&nbsp;<b>{pct(latestValidation.results.control.recall?.rate)}</b>
                {latestValidation.results.control.recall && <>&nbsp;[CI&nbsp;{pct(latestValidation.results.control.recall.low)}–{pct(latestValidation.results.control.recall.high)}]</>}
                &nbsp;·&nbsp;PRECISION&nbsp;<b>{pct(latestValidation.results.control.precision?.rate)}</b>
                {latestValidation.results.elusion && (
                  <>&nbsp;·&nbsp;ELUSION&nbsp;<b>{pct(latestValidation.results.elusion.rate)}</b>
                  &nbsp;(≈{latestValidation.results.elusion.estimated_missed_low}–{latestValidation.results.elusion.estimated_missed_high}&nbsp;OF&nbsp;{latestValidation.results.elusion.null_set_size.toLocaleString()}&nbsp;MISSED)</>
                )}
              </div>
              <div className="def-meta" style={{ marginTop: 4 }}>
                Control set: {latestValidation.results.control.coded} of {latestValidation.results.control.n} coded ·
                agree on responsive {latestValidation.results.control.confusion.tp} · false alarms {latestValidation.results.control.confusion.fp} ·
                missed {latestValidation.results.control.confusion.fn} · agree on non-responsive {latestValidation.results.control.confusion.tn}
              </div>
              {latestValidation.results.control.notes.map((n, i) => (
                <div key={i} className="def-meta" style={{ color: 'var(--color-warning-600)' }}>⚠ {n}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === 'custody' && (
        <div>
          <p className="def-explain">
            Every document's story — where it came from, its hashes, and what happened to it —
            plus an honest ledger of anything that failed processing.
          </p>
          {custody && (
            <div className="def-stats">
              <div className="def-stat"><b>{(custody.loads as unknown[]).length}</b><span>loads</span></div>
              <div className="def-stat"><b>{(custody.documents as { total: number }).total.toLocaleString()}</b><span>documents</span></div>
              <div className="def-stat"><b>{(custody.documents as { hashed_sha256: number }).hashed_sha256.toLocaleString()}</b><span>SHA-256 hashed</span></div>
              <div className="def-stat"><b>{(custody.productions as unknown[]).length}</b><span>production sets</span></div>
              <div className="def-stat"><b>{exceptionsTotal ?? '—'}</b><span>processing exception{exceptionsTotal === 1 ? '' : 's'}</span></div>
            </div>
          )}
          {exceptionsTotal !== null && exceptionsTotal > 0 && (
            <button className="btn btn-secondary btn-sm" style={{ marginTop: 'var(--space-3)' }}
              onClick={() => fetchExceptionsCsv(productionId).then(b => saveBlob(b, 'exceptions_report.csv')).catch(() => showToast('CSV failed', 'error'))}>
              Download exceptions CSV
            </button>
          )}
        </div>
      )}
    </div>
  );
}
