import { auth } from '../firebase';
import type {
  DocumentDetail, DocumentTagEntry, IngestJob, NoteEntry, PaginatedDocuments,
  PendingInviteEntry, ProductionAccessEntry, ProductionInfo,
  SavedSearch, SearchResponse, Tag,
} from '../types';

async function request<T>(url: string, options?: RequestInit): Promise<T> {
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

export function listDocuments(page = 1, perPage = 50, productionId?: number, tagId?: number) {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage) });
  if (productionId) params.set('production_id', String(productionId));
  if (tagId) params.set('tag_id', String(tagId));
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

export const imageUrl = (docId: string, pageNum: number) =>
  `/api/documents/${docId}/image/${pageNum}`;

export const nativeUrl = (docId: string) =>
  `/api/documents/${docId}/native`;

export const streamUrl = (docId: string) =>
  `/api/documents/${docId}/stream`;

// ── Search ──

export async function searchDocuments(
  q: string,
  page = 1,
  perPage = 50,
  sort = 'relevance',
  productionId?: number,
  tagIds?: number[],
  metadata?: Record<string, string>,
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q, page: String(page), per_page: String(perPage), sort });
  if (productionId) params.set('production_id', String(productionId));
  if (tagIds?.length) params.set('tag_ids', tagIds.join(','));
  if (metadata && Object.keys(metadata).length > 0) {
    params.set('metadata', JSON.stringify(metadata));
  }
  return request<SearchResponse>(`/api/search?${params}`);
}

// ── Tags ──

export const getTags = (category?: string) =>
  request<Tag[]>(`/api/tags${category ? `?category=${category}` : ''}`);

export const createTag = (data: { name: string; category: string; color?: string; keyboard_shortcut?: string }) =>
  request<Tag>('/api/tags', json(data));

export const getDocumentTags = (docId: string) =>
  request<DocumentTagEntry[]>(`/api/documents/${docId}/tags`);

export const applyTags = (docId: string, tagIds: number[]) =>
  request<DocumentTagEntry[]>(`/api/documents/${docId}/tags`, json({ tag_ids: tagIds }));

export const removeTag = (docId: string, tagId: number) =>
  request(`/api/documents/${docId}/tags/${tagId}`, { method: 'DELETE' });

export const bulkTag = (docIds: string[], tagIds: number[]) =>
  request<{ tagged: number }>('/api/documents/bulk-tag', json({ doc_ids: docIds, tag_ids: tagIds }));

// ── Notes ──

export const getNotes = (docId: string) =>
  request<NoteEntry[]>(`/api/documents/${docId}/notes`);

export const createNote = (docId: string, content: string) =>
  request<NoteEntry>(`/api/documents/${docId}/notes`, json({ content }));

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

// ── Export ──

export const exportDocsCsvUrl = (productionId?: number, tagId?: number) => {
  const params = new URLSearchParams();
  if (productionId) params.set('production_id', String(productionId));
  if (tagId) params.set('tag_id', String(tagId));
  const qs = params.toString();
  return `/api/export/documents/csv${qs ? `?${qs}` : ''}`;
};

export const exportSearchCsvUrl = (q: string, productionId?: number) => {
  const params = new URLSearchParams({ q });
  if (productionId) params.set('production_id', String(productionId));
  return `/api/export/search/csv?${params}`;
};

export const findSimilar = (docId: string) =>
  request<{ source_id: string; search_terms: string; results: unknown[]; total: number }>(
    `/api/ai/find-similar/${docId}`, { method: 'POST' }
  );

// ── Productions ──

export const listProductions = () =>
  request<ProductionInfo[]>('/api/productions');

export const getProductionAccess = (productionId: number) =>
  request<ProductionAccessEntry[]>(`/api/productions/${productionId}/access`);

export const getProductionInvites = (productionId: number) =>
  request<PendingInviteEntry[]>(`/api/productions/${productionId}/invites`);

export const inviteUser = (productionId: number, email: string) =>
  request<{ status: string; email: string }>(`/api/productions/${productionId}/access`, json({ email }));

export const revokeAccess = (productionId: number, userId: string) =>
  request(`/api/productions/${productionId}/access/${userId}`, { method: 'DELETE' });

// ── Ingest ──

export const startIngest = (productionName: string, description: string, totalFiles: number) =>
  request<IngestJob>('/api/ingest', json({ production_name: productionName, description, total_files: totalFiles }));

export const getIngestStatus = (jobId: string) =>
  request<IngestJob>(`/api/ingest/${jobId}/status`);
