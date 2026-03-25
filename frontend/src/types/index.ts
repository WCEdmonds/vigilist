export interface Tag {
  id: number;
  name: string;
  category: string;
  color: string;
  keyboard_shortcut: string | null;
}

export interface DocumentTagEntry {
  id: number;
  tag: Tag;
  applied_by: string;
  applied_at: string;
}

export interface NoteEntry {
  id: number;
  document_id: string;
  content: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface SavedSearch {
  id: number;
  name: string;
  query: string;
  filters: Record<string, unknown>;
  created_by: string;
  created_at: string;
}

export interface DocumentSummary {
  id: string;
  production_id: number;
  bates_begin: string;
  bates_end: string;
  page_count: number;
  has_native: boolean;
  title: string | null;
  processing_status: string;
  tags: Tag[];
  note_count: number;
}

export interface DocumentDetail {
  id: string;
  production_id: number;
  bates_begin: string;
  bates_end: string;
  page_count: number;
  title: string | null;
  summary: string | null;
  processing_status: string;
  metadata: Record<string, string>;
  text_content: string | null;
  native_path: string | null;
  image_paths: string[];
  tags: DocumentTagEntry[];
  note_count: number;
}

export interface SearchResult {
  id: string;
  production_id: number;
  bates_begin: string;
  bates_end: string;
  page_count: number;
  title: string | null;
  snippet: string;
  rank: number;
  tags: Tag[];
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  page: number;
  per_page: number;
}

export interface PaginatedDocuments {
  documents: DocumentSummary[];
  total: number;
  page: number;
  per_page: number;
}

export interface ProductionInfo {
  id: number;
  name: string;
  description: string | null;
  owner_id: string | null;
  is_owner: boolean;
  created_at: string;
}

export interface ProductionAccessEntry {
  id: number;
  user_id: string;
  user_email: string;
  user_display_name: string | null;
  role: string;
  granted_by: string;
  granted_at: string;
}

export interface PendingInviteEntry {
  id: number;
  email: string;
  invited_by: string;
  created_at: string;
}

export interface IngestJob {
  id: string;
  production_id: number;
  production_name: string;
  status: 'pending' | 'processing' | 'complete' | 'failed';
  total_files: number;
  processed_files: number;
  errors: string[];
  created_at: string;
  completed_at: string | null;
}

export interface AuditLogEntry {
  id: number;
  user_id: string;
  user_email: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  production_id: number | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface PaginatedAuditLogs {
  logs: AuditLogEntry[];
  total: number;
  page: number;
  per_page: number;
}

// ── Review Queues & Batches ──

export interface ReviewQueue {
  id: number;
  production_id: number;
  name: string;
  description: string | null;
  query: string;
  filters: Record<string, unknown>;
  status: string;
  created_by: string;
  created_at: string;
  batch_count: number;
  total_documents: number;
  reviewed_documents: number;
}

export interface ReviewBatch {
  id: number;
  queue_id: number;
  queue_name: string;
  reviewer_id: string | null;
  reviewer_email: string | null;
  status: string;
  size: number;
  reviewed_count: number;
  assigned_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface BatchDocument {
  id: number;
  batch_id: number;
  document_id: string;
  position: number;
  reviewed: string;
  reviewed_at: string | null;
  bates_begin: string;
  title: string | null;
  next_batch_id?: number | null;
}

export interface DashboardStats {
  total_documents: number;
  reviewed_documents: number;
  pending_documents: number;
  percent_complete: number;
  tag_breakdown: Record<string, Record<string, number>>;
  reviewer_stats: { user_id: string; email: string; reviewed_count: number }[];
  queue_stats: { queue_id: number; name: string; total: number; reviewed: number; batch_count: number }[];
}

export interface QCStats {
  total_decisions: number;
  agree_count: number;
  overturn_count: number;
  overturn_rate: number;
  by_reviewer: { reviewer_id: string; email: string; total: number; overturns: number; overturn_rate: number }[];
}

export interface QCContext {
  batch_document_id: number;
  document_id: string;
  bates_begin: string;
  title: string | null;
  original_reviewer_id: string;
  original_reviewer_email: string | null;
  current_tags: { id: number; name: string; category: string }[];
  existing_decision: { id: number; decision: string; reason: string | null; created_at: string } | null;
}

// ── Annotations ──

export interface Annotation {
  id: number;
  document_id: string;
  page_num: number;
  x_pct: number;
  y_pct: number;
  color: string;
  content: string;
  created_by: string;
  created_by_email: string;
  created_by_display_name: string | null;
  created_at: string;
  updated_at: string;
}
