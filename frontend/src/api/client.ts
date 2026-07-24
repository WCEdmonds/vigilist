import { auth } from '../firebase';
import type {
  AIReviewResult, Annotation, BatchDocument, ChipEntity, ClassifyEstimate, ClusterDocument, ClusterInfo, DashboardStats, DocEntity, DocumentDetail, DocumentTagEntry, DuplicateEntry, EntityConnections, EntityListPage, EntityMentionsPage, EntityProfile,
  FamilyThread, GraphData,
  IngestJob, MergeSuggestion, NoteEntry, PaginatedAuditLogs, PaginatedDocuments, PaginatedReviewResults, PendingInviteEntry,
  PipelineInfo, ProductionAccessEntry, ProductionInfo, QCContext, QCStats, ReviewBatch, ReviewProject, ReviewQueue, SavedSearch,
  SearchResponse, SearchResult, Tag, TimelinePage,
} from '../types';

/**
 * Base URL for the AI chat stream. In prod the app talks to the backend
 * through Firebase Hosting's rewrite proxy, which buffers responses and
 * enforces a hard 60s timeout — a tool-using chat turn can exceed that,
 * surfacing as an HTTP 502 even though Cloud Run finishes fine. The chat
 * stream therefore calls the Cloud Run service directly (its CORS config
 * allows the app's domains), which also restores real token streaming.
 * Local dev keeps the relative path through the Vite proxy.
 */
const CHAT_STREAM_BASE = import.meta.env.PROD
  ? 'https://vigilist-api-lhxvmrbzoa-uc.a.run.app'
  : '';

export async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {};

  // Add Firebase Bearer token if user is logged in
  const currentUser = auth.currentUser;
  if (currentUser) {
    const token = await currentUser.getIdToken();
    headers['Authorization'] = `Bearer ${token}`;
  }

  // Merge with any provided headers
  if (options?.headers) {
    Object.assign(headers, options.headers);
  }

  const res = await fetch(url, { ...options, headers });
  if (res.status === 401) {
    throw new Error('Unauthorized');
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

const json = (body: unknown) => ({
  method: 'POST' as const,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

// ── Documents ──

export function getRandomDocument(productionId?: number): Promise<{ id: string }> {
  const params = new URLSearchParams();
  if (productionId) params.set('production_id', String(productionId));
  return request(`/api/documents/random?${params}`);
}


export function listDocuments(page = 1, perPage = 50, productionId?: number, tagId?: number, fileType?: string, sort = 'bates', clusterId?: number, aiDecision?: string, sourceParty?: string, sourceType?: string) {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage), sort });
  if (productionId) params.set('production_id', String(productionId));
  if (tagId) params.set('tag_id', String(tagId));
  if (fileType) params.set('file_type', fileType);
  if (clusterId) params.set('cluster_id', String(clusterId));
  if (aiDecision) params.set('ai_decision', aiDecision);
  if (sourceParty) params.set('source_party', sourceParty);
  if (sourceType) params.set('source_type', sourceType);
  return request<PaginatedDocuments>(`/api/documents?${params}`);
}

export const getSourceParties = (productionId: number) =>
  request<{ source_parties: string[]; undesignated: number }>(`/api/documents/source-parties?production_id=${productionId}`);

export const designateSources = (productionId: number, sourceType: 'collection' | 'received', sourceParty?: string) =>
  request<{ updated: number }>(`/api/productions/${productionId}/source-designation`, json({
    source_type: sourceType,
    source_party: sourceParty || null,
    only_undesignated: true,
  }));

export const getDocument = (id: string) =>
  request<DocumentDetail>(`/api/documents/${id}`);

export const getDocumentNav = (id: string, productionId?: number) =>
  request<{ prev_id: string | null; next_id: string | null }>(
    `/api/documents/${id}/nav${productionId ? `?production_id=${productionId}` : ''}`
  );

export function getByBates(bates: string, productionId?: number) {
  const params = new URLSearchParams({ bates });
  if (productionId) params.set('production_id', String(productionId));
  return request<DocumentDetail>(`/api/documents/by-bates?${params}`);
}


const _imageBlobCache = new Map<string, string>();

export async function fetchImageBlob(docId: string, pageNum: number, width?: number): Promise<string> {
  const key = `${docId}:${pageNum}:${width || 'full'}`;
  const cached = _imageBlobCache.get(key);
  if (cached) return cached;

  const headers: Record<string, string> = {};
  const currentUser = auth.currentUser;
  if (currentUser) {
    const token = await currentUser.getIdToken();
    headers['Authorization'] = `Bearer ${token}`;
  }
  const params = width ? `?w=${width}` : '';
  const res = await fetch(`/api/documents/${docId}/image/${pageNum}${params}`, { headers });
  if (!res.ok) throw new Error(`Image fetch failed: ${res.status}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  _imageBlobCache.set(key, url);
  if (_imageBlobCache.size > 200) {
    const oldest = _imageBlobCache.keys().next().value!;
    URL.revokeObjectURL(_imageBlobCache.get(oldest)!);
    _imageBlobCache.delete(oldest);
  }
  return url;
}


export function updateDocTitle(docId: string, title: string): Promise<{ ok: boolean; title: string | null }> {
  return request(`/api/documents/${docId}/title`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) });
}

export function getNativeUrl(docId: string, download = false): Promise<{ url: string; extension: string; filename: string }> {
  const qs = download ? '?download=true' : '';
  return request(`/api/documents/${docId}/native-url${qs}`);
}

export async function fetchDocumentPdf(docId: string): Promise<Blob> {
  const headers: Record<string, string> = {};
  const currentUser = auth.currentUser;
  if (currentUser) {
    const token = await currentUser.getIdToken();
    headers['Authorization'] = `Bearer ${token}`;
  }
  const res = await fetch(`/api/documents/${docId}/pdf`, { headers });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* Response body not JSON — use default error detail */
    }
    throw new Error(detail);
  }
  return res.blob();
}


export async function fetchBulkZip(docIds: string[]): Promise<Blob> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const currentUser = auth.currentUser;
  if (currentUser) {
    const token = await currentUser.getIdToken();
    headers['Authorization'] = `Bearer ${token}`;
  }
  const res = await fetch('/api/documents/bulk-zip', {
    method: 'POST',
    headers,
    body: JSON.stringify({ document_ids: docIds }),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* Response body not JSON — use default error detail */
    }
    throw new Error(detail);
  }
  return res.blob();
}


// ── Search ──

export async function searchDocuments(
  q: string,
  page = 1,
  perPage = 50,
  sort = 'relevance',
  productionId?: number,
  metadata?: Record<string, string>,
  mode?: 'fulltext' | 'semantic',
  fileType?: string,
  sourceParty?: string,
  sourceType?: string,
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q, page: String(page), per_page: String(perPage), sort });
  if (productionId) params.set('production_id', String(productionId));
  if (metadata && Object.keys(metadata).length > 0) {
    params.set('metadata', JSON.stringify(metadata));
  }
  if (mode) params.set('mode', mode);
  if (fileType) params.set('file_type', fileType);
  if (sourceParty) params.set('source_party', sourceParty);
  if (sourceType) params.set('source_type', sourceType);
  return request<SearchResponse>(`/api/search?${params}`);
}

// ── Tags ──

export const getTags = (category?: string) =>
  request<Tag[]>(`/api/tags${category ? `?category=${category}` : ''}`);

export const createTag = (data: { name: string; category: string; color?: string; keyboard_shortcut?: string }) =>
  request<Tag>('/api/tags', json(data));


export const applyTags = (docId: string, tagIds: number[]) =>
  request<DocumentTagEntry[]>(`/api/documents/${docId}/tags`, json({ tag_ids: tagIds }));

export const removeTag = (docId: string, tagId: number) =>
  request(`/api/documents/${docId}/tags/${tagId}`, { method: 'DELETE' });

export const bulkTag = (docIds: string[], tagIds: number[]) =>
  request<{ tagged: number }>('/api/documents/bulk-tag', json({ doc_ids: docIds, tag_ids: tagIds }));

// ── Notes ──

export const getNotes = (docId: string) =>
  request<NoteEntry[]>(`/api/documents/${docId}/notes`);

export const createNote = (docId: string, content: string, timestamp?: number) =>
  request<NoteEntry>(`/api/documents/${docId}/notes`, json({ content, timestamp: timestamp ?? null }));

export const updateNote = (noteId: number, content: string) =>
  request<NoteEntry>(`/api/notes/${noteId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }) });

export const deleteNote = (noteId: number) =>
  request(`/api/notes/${noteId}`, { method: 'DELETE' });

// ── Saved Searches ──

export const getSavedSearches = () =>
  request<SavedSearch[]>('/api/saved-searches');

export const createSavedSearch = (name: string, query: string, filters: Record<string, unknown> = {}) =>
  request<SavedSearch>('/api/saved-searches', json({ name, query, filters }));

export const deleteSavedSearch = (id: number) =>
  request(`/api/saved-searches/${id}`, { method: 'DELETE' });

// ── AI Features ──

export const summarizeDocument = (docId: string) =>
  request<{ summary: string; cached: boolean }>(`/api/ai/summarize/${docId}`, { method: 'POST' });

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

/**
 * Stream a chat response from the AI agent. Calls `onDelta` for each streamed
 * text chunk and `onError` with a message on failure. Resolves when the stream
 * completes (or errors). Pass an AbortSignal to cancel an in-flight response.
 */
export async function streamChat(
  messages: ChatMessage[],
  docIds: string[],
  handlers: {
    onDelta: (text: string) => void;
    onError: (message: string) => void;
    onToolUse?: (evt: { name: string; summary: string }) => void;
    onToolResult?: (evt: { name: string; ok: boolean; summary: string }) => void;
  },
  signal?: AbortSignal,
  productionId?: number,
): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const currentUser = auth.currentUser;
  if (currentUser) {
    const token = await currentUser.getIdToken();
    headers['Authorization'] = `Bearer ${token}`;
  }

  let res: Response;
  try {
    res = await fetch(`${CHAT_STREAM_BASE}/api/ai/chat`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ messages, doc_ids: docIds, production_id: productionId }),
      signal,
    });
  } catch (e: unknown) {
    if (e instanceof Error && e.name === 'AbortError') return;
    handlers.onError(e instanceof Error ? e.message : 'Network error');
    return;
  }

  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* Response body not JSON — use default error detail */
    }
    handlers.onError(detail);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep: number;
      // SSE frames are separated by a blank line.
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const dataLine = frame.split('\n').find(l => l.startsWith('data:'));
        if (!dataLine) continue;
        const payload = dataLine.slice(5).trim();
        if (!payload) continue;
        try {
          const evt = JSON.parse(payload);
          if (evt.type === 'delta' && typeof evt.text === 'string') handlers.onDelta(evt.text);
          else if (evt.type === 'tool_use') handlers.onToolUse?.({ name: evt.name, summary: evt.summary });
          else if (evt.type === 'tool_result') handlers.onToolResult?.({ name: evt.name, ok: !!evt.ok, summary: evt.summary });
          else if (evt.type === 'error') handlers.onError(evt.message || 'The AI service failed to respond.');
        } catch {
          /* Ignore malformed frames */
        }
      }
    }
  } catch (e: unknown) {
    if (!(e instanceof Error && e.name === 'AbortError')) {
      handlers.onError(e instanceof Error ? e.message : 'Stream interrupted');
    }
  }
}

// ── Export ──

async function downloadCsv(url: string, filename: string) {
  const headers: Record<string, string> = {};
  const currentUser = auth.currentUser;
  if (currentUser) {
    const token = await currentUser.getIdToken();
    headers['Authorization'] = `Bearer ${token}`;
  }
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`Export failed: ${res.status}`);
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

export async function exportDocsCsv(productionId?: number, tagId?: number) {
  const params = new URLSearchParams();
  if (productionId) params.set('production_id', String(productionId));
  if (tagId) params.set('tag_id', String(tagId));
  const qs = params.toString();
  await downloadCsv(`/api/export/documents/csv${qs ? `?${qs}` : ''}`, 'documents.csv');
}

export async function exportSearchCsv(q: string, productionId?: number) {
  const params = new URLSearchParams({ q });
  if (productionId) params.set('production_id', String(productionId));
  await downloadCsv(`/api/export/search/csv?${params}`, 'search_results.csv');
}

export const findSimilar = (docId: string) =>
  request<{ source_id: string; search_terms: string; results: SearchResult[]; total: number }>(
    `/api/ai/find-similar/${docId}`, { method: 'POST' }
  );

// ── Productions ──

export const listProductions = () =>
  request<ProductionInfo[]>('/api/productions');

export const deleteProduction = (productionId: number) =>
  request<{ ok: boolean }>(`/api/productions/${productionId}`, { method: 'DELETE' });

export const getProductionAccess = (productionId: number) =>
  request<ProductionAccessEntry[]>(`/api/productions/${productionId}/access`);

export const getProductionInvites = (productionId: number) =>
  request<PendingInviteEntry[]>(`/api/productions/${productionId}/invites`);

export async function inviteUser(productionId: number, email: string, role = 'reviewer') {
  return request<{ status: string; email: string }>(`/api/productions/${productionId}/access`, json({ email, role }));
}

export const revokeAccess = (productionId: number, userId: string) =>
  request(`/api/productions/${productionId}/access/${userId}`, { method: 'DELETE' });

// ── Audit Log ──

export async function getAuditLogs(
  page = 1,
  perPage = 50,
  productionId?: number,
  userId?: string,
  action?: string,
  dateFrom?: string,
  dateTo?: string,
): Promise<PaginatedAuditLogs> {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage) });
  if (productionId) params.set('production_id', String(productionId));
  if (userId) params.set('user_id', userId);
  if (action) params.set('action', action);
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
  return request<PaginatedAuditLogs>(`/api/audit?${params}`);
}

// ── Ingest ──

export const createProductionForIngest = (productionName: string, description: string, caseContext: string) =>
  request<{ production_id: number; production_name: string }>('/api/ingest/create', json({ production_name: productionName, description, case_context: caseContext }));

export interface ProposedColumn {
  source_name: string;
  samples: string[];
  target: string | null;
  confidence: number;
  source: 'alias' | 'ai' | 'unmapped';
}

export const analyzeLoadFile = (productionId: number, loadId?: string) =>
  request<{
    format: { encoding: string; delimiter: string };
    columns: ProposedColumn[];
    sample_rows: Record<string, string>[];
    total_rows: number;
  }>(
    '/api/ingest/analyze', json({ production_id: productionId, load_id: loadId }),
  );

export const startProcessing = (
  productionId: number,
  totalFiles: number,
  sourceFormat: 'relativity' | 'generic_pdf' | 'native' = 'relativity',
  fieldMapping: Record<string, string> = {},
  custodian: string = '',
  sourceParty: string = '',
  sourceType: 'collection' | 'received' = 'collection',
  loadId?: string,
) =>
  request<IngestJob>('/api/ingest/process', json({
    production_id: productionId,
    total_files: totalFiles,
    source_format: sourceFormat,
    field_mapping: fieldMapping,
    custodian,
    source_party: sourceParty,
    source_type: sourceType,
    load_id: loadId,
  }));

export const getPipeline = (productionId: number): Promise<PipelineInfo> =>
  request(`/api/productions/${productionId}/pipeline`);

export interface IntakeSummary {
  documents: number;
  custodians: number;
  email_families: number;
  family_documents: number;
  threads: number;
  inclusive_emails: number;
  duplicate_groups: number;
}

export const getIntakeSummary = (productionId: number): Promise<IntakeSummary> =>
  request(`/api/productions/${productionId}/intake-summary`);

export const runPipeline = (productionId: number, force = false) =>
  request<{ started: boolean }>(`/api/productions/${productionId}/pipeline/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force }),
  });

export const updateProduction = (productionId: number, data: { description?: string; case_context?: string }): Promise<ProductionInfo> =>
  request(`/api/productions/${productionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

export const getClusterDocuments = (productionId: number, clusterId: number, limit = 5): Promise<ClusterDocument[]> =>
  request(`/api/productions/${productionId}/clusters/${clusterId}/documents?limit=${limit}`);

export const getIngestStatus = (jobId: string) =>
  request<IngestJob>(`/api/ingest/${jobId}/status`);

// ── Review Queues ──

export async function listQueues(productionId: number): Promise<ReviewQueue[]> {
  return request<ReviewQueue[]>(`/api/productions/${productionId}/queues`);
}

export async function createQueue(productionId: number, name: string, description = '', query = '', filters: Record<string, unknown> = {}): Promise<ReviewQueue> {
  return request<ReviewQueue>(`/api/productions/${productionId}/queues`, json({ name, description, query, filters }));
}

export async function deleteQueue(productionId: number, queueId: number): Promise<void> {
  await request(`/api/productions/${productionId}/queues/${queueId}`, { method: 'DELETE' });
}

export async function createBatches(productionId: number, queueId: number, batchSize = 50, reviewerId?: string): Promise<ReviewBatch[]> {
  return request<ReviewBatch[]>(`/api/productions/${productionId}/queues/${queueId}/batches`, json({ batch_size: batchSize, reviewer_id: reviewerId }));
}

export async function listQueueBatches(productionId: number, queueId: number): Promise<ReviewBatch[]> {
  return request<ReviewBatch[]>(`/api/productions/${productionId}/queues/${queueId}/batches`);
}

// ── Batches ──

export async function getMyBatches(productionId?: number): Promise<ReviewBatch[]> {
  const params = new URLSearchParams();
  if (productionId) params.set('production_id', String(productionId));
  return request<ReviewBatch[]>(`/api/batches/my?${params}`);
}

export async function getBatch(batchId: number): Promise<ReviewBatch> {
  return request<ReviewBatch>(`/api/batches/${batchId}`);
}

export async function assignBatch(batchId: number, reviewerId: string): Promise<ReviewBatch> {
  return request<ReviewBatch>(`/api/batches/${batchId}/assign`, json({ reviewer_id: reviewerId }));
}

export async function listBatchDocuments(batchId: number): Promise<BatchDocument[]> {
  return request<BatchDocument[]>(`/api/batches/${batchId}/documents`);
}

export async function updateBatchDocument(batchId: number, docId: string, reviewed: string): Promise<BatchDocument & { next_batch_id: number | null }> {
  return request(`/api/batches/${batchId}/documents/${docId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reviewed }) });
}

// ── Dashboard ──

export async function getDashboard(productionId: number): Promise<DashboardStats> {
  return request<DashboardStats>(`/api/productions/${productionId}/dashboard`);
}

export async function getQCStats(productionId: number): Promise<QCStats> {
  return request<QCStats>(`/api/productions/${productionId}/dashboard/qc`);
}

// ── QC ──

export async function createQCSample(queueId: number, samplePercent = 10, reviewerId?: string): Promise<number[]> {
  return request<number[]>('/api/qc/sample', json({ queue_id: queueId, sample_percent: samplePercent, reviewer_id: reviewerId }));
}

export async function getQCContext(bdId: number): Promise<QCContext> {
  return request<QCContext>(`/api/qc/batch-document/${bdId}`);
}

export async function recordQCDecision(bdId: number, decision: string, reason?: string, newTagIds?: number[]): Promise<unknown> {
  return request(`/api/qc/batch-document/${bdId}/decide`, json({ decision, reason, new_tag_ids: newTagIds }));
}

// ── Annotations ──

export function listAnnotations(docId: string): Promise<Annotation[]> {
  return request<Annotation[]>(`/api/documents/${docId}/annotations`);
}

export function createAnnotation(docId: string, pageNum: number, xPct: number, yPct: number, color: string, content = ''): Promise<Annotation> {
  return request<Annotation>(`/api/documents/${docId}/annotations`, json({ page_num: pageNum, x_pct: xPct, y_pct: yPct, color, content }));
}

export function updateAnnotation(annId: number, data: { content?: string; color?: string }): Promise<Annotation> {
  return request<Annotation>(`/api/annotations/${annId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
}

export function deleteAnnotation(annId: number): Promise<void> {
  return request(`/api/annotations/${annId}`, { method: 'DELETE' });
}

// ── AI Review ──

export const listReviewProjects = (productionId: number) =>
  request<ReviewProject[]>(`/api/review/projects/${productionId}`);

export const createReviewProject = (productionId: number, data: { name: string; prompt_text: string; sample_size?: number; categories?: { name: string; color: string; description: string }[] }) =>
  request<ReviewProject>(`/api/review/projects/${productionId}`, json(data));


export const updateReviewProject = (productionId: number, projectId: number, data: { name?: string; prompt_text?: string; sample_size?: number; agreement_threshold?: number; is_primary?: boolean }) =>
  request<ReviewProject>(`/api/review/projects/${productionId}/${projectId}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

export const deleteReviewProject = (productionId: number, projectId: number) =>
  request(`/api/review/projects/${productionId}/${projectId}`, { method: 'DELETE' });

export const runSample = (productionId: number, projectId: number) =>
  request<{ status: string; sample_size: number }>(`/api/review/projects/${productionId}/${projectId}/sample`, { method: 'POST' });

export const runFull = (productionId: number, projectId: number) =>
  request<{ status: string; remaining: number }>(`/api/review/projects/${productionId}/${projectId}/run`, { method: 'POST' });

export const pauseRun = (productionId: number, projectId: number) =>
  request(`/api/review/projects/${productionId}/${projectId}/pause`, { method: 'POST' });

export const getProjectStatus = (productionId: number, projectId: number) =>
  request<{ status: string; total_documents: number; processed_documents: number; total_cost_tokens: number }>(
    `/api/review/projects/${productionId}/${projectId}/status`
  );

export const listReviewResults = (
  productionId: number, projectId: number,
  page = 1, perPage = 50, sort = 'confidence_asc',
  options?: { decision_filter?: string; sample_only?: boolean; needs_review_only?: boolean },
) => {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage), sort });
  if (options?.decision_filter) params.set('decision_filter', options.decision_filter);
  if (options?.sample_only) params.set('sample_only', 'true');
  if (options?.needs_review_only) params.set('needs_review_only', 'true');
  return request<PaginatedReviewResults>(`/api/review/projects/${productionId}/${projectId}/results?${params}`);
};

export const recordDecision = (resultId: number, decision: string, note?: string) =>
  request<AIReviewResult>(`/api/review/results/${resultId}/decide`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, note }),
  });

export const getClassifyEstimate = (productionId: number): Promise<ClassifyEstimate> =>
  request(`/api/review/estimate/${productionId}`);

export const startAutoClassification = (productionId: number) =>
  request(`/api/review/auto-classify/${productionId}`, { method: 'POST' });

export const bulkAcceptResults = (productionId: number, projectId: number, minConfidence: number): Promise<{ accepted: number }> =>
  request(`/api/review/projects/${productionId}/${projectId}/bulk-accept`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ min_confidence: minConfidence }),
  });

// ── Intelligence ──

export function getClusters(productionId: number): Promise<ClusterInfo[]> {
  return request<ClusterInfo[]>(`/api/productions/${productionId}/clusters`);
}

export function getDocumentDuplicates(docId: string): Promise<DuplicateEntry[]> {
  return request<DuplicateEntry[]>(`/api/documents/${docId}/duplicates`);
}

export function getDocumentFamily(docId: string): Promise<FamilyThread> {
  return request<FamilyThread>(`/api/documents/${docId}/family`);
}


// ── Production sets (P2) ──

export interface ProductionSetInfo {
  id: number;
  production_id: number;
  name: string;
  status: 'draft' | 'locked';
  prefix: string;
  padding: number;
  start_number: number;
  sort_key: string;
  designation: string | null;
  created_by: string;
  created_at: string;
  locked_by: string | null;
  locked_at: string | null;
  doc_count: number;
  page_count: number | null;
  bates_begin: string | null;
  bates_end: string | null;
  render_status: string;
  render_error: string | null;
  rendered_at: string | null;
  rendered_count: number;
  package_status: string;
  package_error: string | null;
  package_path: string | null;
  packaged_at: string | null;
  conflicts_overridden_by: string | null;
  conflicts_overridden_at: string | null;
  image_format: string;
  native_file_types: string[];
  volume_max_mb: number | null;
}

export interface ProductionSetMember {
  document_id: string;
  control_number: string;
  sort_order: number | null;
  bates_begin: string | null;
  bates_end: string | null;
  pages: number | null;
  disposition: string | null;
  designation: string | null;
}

export interface ValidationConflict {
  document_id: string;
  control_number: string;
  detail: string;
}

export interface ValidationReport {
  qc_pending: ValidationConflict[];
  privilege_produce: ValidationConflict[];
  no_images: ValidationConflict[];
  received_document: ValidationConflict[];
  total: number;
}

export interface ManifestReport {
  production_set: Record<string, unknown>;
  counts: Record<string, number>;
  bates_range: { begin: string | null; end: string | null };
  continuity: { ok: boolean; errors: string[] };
  artifacts: { bates_begin: string; path: string | null; sha256?: string; bytes?: number }[];
  generated_at: string;
}

export const listProductionSets = (productionId: number) =>
  request<ProductionSetInfo[]>(`/api/productions/${productionId}/production-sets`);

export const createProductionSet = (productionId: number, body: {
  name: string; prefix: string; padding?: number; start_number?: number;
  sort_key?: string; designation?: string | null;
  image_format?: string; native_file_types?: string[]; volume_max_mb?: number | null;
}) => request<ProductionSetInfo>(`/api/productions/${productionId}/production-sets`, json(body));

export const getProductionSet = (setId: number) =>
  request<ProductionSetInfo>(`/api/production-sets/${setId}`);

export const getProductionSetMembers = (setId: number) =>
  request<ProductionSetMember[]>(`/api/production-sets/${setId}/documents`);

export const addProductionSetDocuments = (setId: number, body: {
  document_ids?: string[]; tag_id?: number; include_families?: boolean;
  exclude_duplicates?: boolean; exclude_received?: boolean;
}) => request<{ added: number; skipped_existing: number; skipped_duplicates: number; families_added: number; skipped_received: number }>(
  `/api/production-sets/${setId}/documents`, json(body));

export const removeProductionSetDocuments = (setId: number, documentIds: string[]) =>
  request<{ removed: number }>(`/api/production-sets/${setId}/documents`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document_ids: documentIds }),
  });

export const deleteProductionSet = (setId: number) =>
  request<{ ok: boolean }>(`/api/production-sets/${setId}`, { method: 'DELETE' });

export const getProductionSetValidation = (setId: number) =>
  request<ValidationReport>(`/api/production-sets/${setId}/validation`);

export const lockProductionSet = (setId: number, overrideConflicts = false) =>
  request<{ doc_count: number; page_count: number; bates_begin: string; bates_end: string }>(
    `/api/production-sets/${setId}/lock`, json({ override_conflicts: overrideConflicts }));

export const renderProductionSet = (setId: number) =>
  request<{ documents: number; batches: number }>(`/api/production-sets/${setId}/render`, { method: 'POST' });

export const getProductionSetManifest = (setId: number) =>
  request<ManifestReport>(`/api/production-sets/${setId}/manifest`);

export const packageProductionSet = (setId: number) =>
  request<{ documents: number }>(`/api/production-sets/${setId}/package`, { method: 'POST' });

async function authedBlob(url: string): Promise<Blob> {
  const headers: Record<string, string> = {};
  const currentUser = auth.currentUser;
  if (currentUser) {
    const token = await currentUser.getIdToken();
    headers['Authorization'] = `Bearer ${token}`;
  }
  const res = await fetch(url, { headers });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* Response body not JSON — use default error detail */
    }
    throw new Error(detail);
  }
  return res.blob();
}

export const fetchProducedPdf = (setId: number, documentId: string) =>
  authedBlob(`/api/production-sets/${setId}/documents/${documentId}/pdf`);

export const fetchProductionPackage = (setId: number) =>
  authedBlob(`/api/production-sets/${setId}/package`);

// ── Ontology / entities ──

export const getDocumentEntities = (docId: string) =>
  request<{ entities: DocEntity[] }>(`/api/documents/${docId}/entities`);

export const getEntity = (entityId: string) =>
  request<EntityProfile>(`/api/entities/${entityId}`);

export const getEntityMentions = (entityId: string, page = 1, perPage = 20) =>
  request<EntityMentionsPage>(`/api/entities/${entityId}/mentions?page=${page}&per_page=${perPage}`);

export const getEntityConnections = (entityId: string) =>
  request<EntityConnections>(`/api/entities/${entityId}/connections`);

export function listEntities(productionId: number, search?: string, entityType?: string, page = 1, perPage = 50) {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage) });
  if (search) params.set('search', search);
  if (entityType) params.set('entity_type', entityType);
  return request<EntityListPage>(`/api/productions/${productionId}/entities?${params}`);
}

export const listMergeSuggestions = (productionId: number, status = 'pending') =>
  request<MergeSuggestion[]>(`/api/productions/${productionId}/merge-suggestions?status=${status}`);

export const acceptMergeSuggestion = (suggestionId: number) =>
  request<{ merge_id: number; winner_id: string }>(`/api/merge-suggestions/${suggestionId}/accept`, { method: 'POST' });

export const rejectMergeSuggestion = (suggestionId: number) =>
  request<{ ok: boolean }>(`/api/merge-suggestions/${suggestionId}/reject`, { method: 'POST' });

export const mergeEntities = (winnerId: string, loserId: string) =>
  request<{ merge_id: number; winner_id: string }>(`/api/entities/merge`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ winner_id: winnerId, loser_id: loserId }) });

export const autoResolveTypos = (productionId: number) =>
  request<{ merged: number }>(`/api/productions/${productionId}/merge-suggestions/auto-resolve-typos`, { method: 'POST' });

export const triggerEntityExtraction = (productionId: number, rebuild = false) =>
  request<{ status: string }>(`/api/productions/${productionId}/extract-entities${rebuild ? '?rebuild=true' : ''}`, { method: 'POST' });

export function getTimeline(productionId: number, entityId?: string, eventType?: string, page = 1, perPage = 50) {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage) });
  if (entityId) params.set('entity_id', entityId);
  if (eventType) params.set('event_type', eventType);
  return request<TimelinePage>(`/api/productions/${productionId}/timeline?${params}`);
}

export const getGraph = (productionId: number, maxNodes = 75, minSharedDocs = 2) =>
  request<GraphData>(`/api/productions/${productionId}/graph?max_nodes=${maxNodes}&min_shared_docs=${minSharedDocs}`);

export const getEntitiesSummary = (ids: string[]) =>
  request<{ summaries: Record<string, ChipEntity[]> }>(`/api/entities-summary?ids=${ids.join(',')}`);
// ── Defensibility (Phase 3) ──

export interface SearchTermReportInfo {
  id: number;
  production_id: number;
  name: string;
  terms: string[];
  results: {
    total_docs: number; any_hits: number; any_with_families: number;
    source_type: string | null;
    terms: { term: string; hits: number; with_families: number; unique_hits: number }[];
  } | null;
  computed_at: string | null;
  created_by: string;
  created_at: string;
}

export interface SampleInfo {
  id: number;
  production_id: number;
  name: string;
  purpose: string;
  params: Record<string, unknown>;
  document_ids: string[];
  created_by: string;
  created_at: string;
}

export interface SampleEstimate {
  n: number; positives: number; confidence: number;
  rate: number; ci_low: number; ci_high: number;
  population: number; estimated_low: number; estimated_high: number;
}

export interface TarValidationInfo {
  id: number;
  production_id: number;
  project_id: number;
  params: Record<string, unknown>;
  results: {
    confidence: number;
    control: {
      n: number; coded: number; uncoded: number; conflicted: number;
      machine_undecided: number;
      confusion: { tp: number; fp: number; fn: number; tn: number };
      richness: { rate: number; low: number; high: number } | null;
      recall: { rate: number; low: number; high: number } | null;
      precision: { rate: number; low: number; high: number } | null;
      notes: string[];
    };
    elusion: {
      n: number; positives: number; rate: number; low: number; high: number;
      null_set_size: number; estimated_missed_low: number; estimated_missed_high: number;
    } | null;
  };
  created_by: string;
  created_at: string;
}

export const listSearchTermReports = (productionId: number) =>
  request<SearchTermReportInfo[]>(`/api/productions/${productionId}/search-term-reports`);

export const createSearchTermReport = (productionId: number, name: string, terms: string[]) =>
  request<SearchTermReportInfo>(`/api/productions/${productionId}/search-term-reports`, json({ name, terms }));

export const runSearchTermReport = (reportId: number, sourceType?: string) =>
  request<SearchTermReportInfo['results']>(`/api/search-term-reports/${reportId}/run`, json({ source_type: sourceType || null }));

export const fetchSearchTermReportCsv = (reportId: number) =>
  authedBlob(`/api/search-term-reports/${reportId}/csv`);

export const listSamples = (productionId: number) =>
  request<SampleInfo[]>(`/api/productions/${productionId}/samples`);

export const drawSample = (productionId: number, body: {
  name: string; purpose: string; size?: number | null;
  source_type?: string | null; scope?: string | null; project_id?: number | null;
}) => request<SampleInfo>(`/api/productions/${productionId}/samples`, json(body));

export const getSampleEstimate = (sampleId: number, tagId: number) =>
  request<SampleEstimate>(`/api/samples/${sampleId}/estimate?tag_id=${tagId}`);

export const listTarValidations = (productionId: number) =>
  request<TarValidationInfo[]>(`/api/productions/${productionId}/tar-validation`);

export const runTarValidation = (productionId: number, body: {
  project_id: number; control_sample_id: number; responsive_tag_id: number;
  nonresponsive_tag_id?: number | null; elusion_sample_id?: number | null;
  confidence?: number;
}) => request<TarValidationInfo>(`/api/productions/${productionId}/tar-validation`, json(body));

export const getChainOfCustody = (productionId: number) =>
  request<Record<string, unknown>>(`/api/productions/${productionId}/chain-of-custody`);

export const getExceptionsReport = (productionId: number) =>
  request<{ total: number; counts: Record<string, number> }>(`/api/productions/${productionId}/exceptions`);

export const fetchExceptionsCsv = (productionId: number) =>
  authedBlob(`/api/productions/${productionId}/exceptions/csv`);
