import { auth } from '../firebase';
import type {
  AIReviewResult, Annotation, BatchDocument, ClusterInfo, DashboardStats, DocumentDetail, DocumentTagEntry, DuplicateEntry,
  FamilyThread,
  IngestJob, NoteEntry, PaginatedAuditLogs, PaginatedDocuments, PaginatedReviewResults, PendingInviteEntry,
  ProductionAccessEntry, ProductionInfo, QCContext, QCStats, ReviewBatch, ReviewProject, ReviewQueue, SavedSearch,
  SearchResponse, SearchResult, Tag,
} from '../types';

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


export function listDocuments(page = 1, perPage = 50, productionId?: number, tagId?: number, fileType?: string, sort = 'bates', clusterId?: number) {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage), sort });
  if (productionId) params.set('production_id', String(productionId));
  if (tagId) params.set('tag_id', String(tagId));
  if (fileType) params.set('file_type', fileType);
  if (clusterId) params.set('cluster_id', String(clusterId));
  return request<PaginatedDocuments>(`/api/documents?${params}`);
}

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
    } catch {}
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
    } catch {}
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
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q, page: String(page), per_page: String(perPage), sort });
  if (productionId) params.set('production_id', String(productionId));
  if (metadata && Object.keys(metadata).length > 0) {
    params.set('metadata', JSON.stringify(metadata));
  }
  if (mode) params.set('mode', mode);
  if (fileType) params.set('file_type', fileType);
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

export const nlSearch = (query: string) =>
  request<{ original_query: string; structured_query: string; results: unknown[]; total: number }>(
    '/api/ai/nl-search', json({ query })
  );

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
): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const currentUser = auth.currentUser;
  if (currentUser) {
    const token = await currentUser.getIdToken();
    headers['Authorization'] = `Bearer ${token}`;
  }

  let res: Response;
  try {
    res = await fetch('/api/ai/chat', {
      method: 'POST',
      headers,
      body: JSON.stringify({ messages, doc_ids: docIds }),
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
    } catch {}
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
          // Ignore malformed frames.
        }
      }
    }
  } catch (e: unknown) {
    if (!(e instanceof Error && e.name === 'AbortError')) handlers.onError(e instanceof Error ? e.message : 'Stream interrupted');
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

export const createProductionForIngest = (productionName: string, description: string) =>
  request<{ production_id: number; production_name: string }>('/api/ingest/create', json({ production_name: productionName, description }));

export interface ProposedColumn {
  source_name: string;
  samples: string[];
  target: string | null;
  confidence: number;
  source: 'alias' | 'ai' | 'unmapped';
}

export const analyzeLoadFile = (productionId: number) =>
  request<{
    format: { encoding: string; delimiter: string };
    columns: ProposedColumn[];
    sample_rows: Record<string, string>[];
    total_rows: number;
  }>(
    '/api/ingest/analyze', json({ production_id: productionId }),
  );

export const startProcessing = (
  productionId: number,
  totalFiles: number,
  sourceFormat: 'relativity' | 'generic_pdf' | 'native' = 'relativity',
  fieldMapping: Record<string, string> = {},
  custodian: string = '',
) =>
  request<IngestJob>('/api/ingest/process', json({
    production_id: productionId,
    total_files: totalFiles,
    source_format: sourceFormat,
    field_mapping: fieldMapping,
    custodian,
  }));


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


// ── Intelligence ──

export function detectDuplicates(productionId: number): Promise<{ status: string; exact_groups: number; similar_groups: number; total_documents_grouped: number }> {
  return request(`/api/productions/${productionId}/detect-duplicates`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
}

export function clusterProduction(productionId: number): Promise<{ status: string; clusters: ClusterInfo[] }> {
  return request(`/api/productions/${productionId}/cluster`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
}

export function getClusters(productionId: number): Promise<ClusterInfo[]> {
  return request<ClusterInfo[]>(`/api/productions/${productionId}/clusters`);
}

export function getDocumentDuplicates(docId: string): Promise<DuplicateEntry[]> {
  return request<DuplicateEntry[]>(`/api/documents/${docId}/duplicates`);
}

export function getDocumentFamily(docId: string): Promise<FamilyThread> {
  return request<FamilyThread>(`/api/documents/${docId}/family`);
}

