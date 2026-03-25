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
