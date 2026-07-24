export interface AttachedDoc {
  id: string;
  label: string;
}

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
  timestamp: number | null;
  created_by: string;
  created_by_email: string;
  created_by_display_name: string | null;
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
  file_type: string;
  title: string | null;
  processing_status: string;
  tags: Tag[];
  note_count: number;
  cluster_id?: number | null;
  cluster_label?: string | null;
  ai_decision?: string | null;
  ai_confidence?: number | null;
  ai_decided?: boolean;
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
  document_count: number;
  case_context?: string | null;
  has_brief?: boolean;
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
  skipped_files: number;
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

// ── AI Review ──

export interface ReviewProject {
  id: number;
  production_id: number;
  name: string;
  prompt_text: string;
  prompt_versions: { version: number; text: string; created_at: string }[];
  categories: { name: string; color: string; description: string }[];
  sample_size: number;
  agreement_threshold: number;
  status: string;
  total_documents: number;
  processed_documents: number;
  total_cost_tokens: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  sample_agreement_rate: number | null;
  decision_breakdown: Record<string, number> | null;
  is_primary?: boolean;
}

export interface ClassifyEstimate {
  doc_count: number;
  est_input_tokens: number;
  est_output_tokens: number;
  est_usd: number;
}

export interface AIReviewResult {
  id: number;
  project_id: number;
  document_id: string;
  bates_begin: string | null;
  title: string | null;
  is_sample: number;
  ai_decision: string;
  confidence_score: number;
  reasoning: string;
  key_excerpts: { text: string; start_offset: number; end_offset: number }[];
  considerations: string | null;
  attorney_decision: string | null;
  attorney_note: string | null;
  prompt_version: number;
  api_model: string;
  api_cost_tokens: number;
  created_at: string;
}

export interface PaginatedReviewResults {
  results: AIReviewResult[];
  total: number;
  page: number;
  per_page: number;
  agreement_rate: number | null;
}

// ── Intelligence ──

export interface ClusterInfo {
  id: number;
  cluster_index: number;
  label: string | null;
  doc_count: number;
  page_count: number;
}

export interface DuplicateEntry {
  document_id: string;
  bates_begin: string;
  title: string | null;
  similarity: number;
  type: string;
  custodian: string | null;
}

export interface FamilyMember {
  document_id: string;
  bates_begin: string;
  title: string | null;
  is_inclusive: boolean;
}

export interface FamilyThread {
  family: FamilyMember[];
  thread: FamilyMember[];
}

export interface ProductionBriefData {
  overview: string;
  key_players: string[];
  date_range: string | null;
  notable_documents: { bates: string; reason: string }[];
  generated_at: string;
  model: string;
}

export type PipelineStageState = 'pending' | 'running' | 'done' | 'failed';

export interface PipelineStatus {
  clustering?: PipelineStageState;
  summaries?: PipelineStageState;
  brief?: PipelineStageState;
  errors?: Record<string, string>;
  updated_at?: string;
}

export interface PipelineInfo {
  status: PipelineStatus | null;
  brief: ProductionBriefData | null;
  case_context: string | null;
  /** Documents summarized so far / total — progress for the summaries stage. */
  summarized_count?: number;
  doc_count?: number;
  key_players_resolved?: { name: string; entity_id: string | null }[] | null;
}

export interface ClusterDocument {
  document_id: string;
  bates_begin: string;
  title: string | null;
}

// ── Ontology ──

export interface EntityMentionSpan {
  surface_text: string;
  start_offset: number | null;
  end_offset: number | null;
}

export interface DocEntity {
  id: string;
  entity_type: 'person' | 'org';
  canonical_name: string;
  mention_count: number;
  mentions: EntityMentionSpan[];
}

export interface EntityProfile {
  id: string;
  production_id: number;
  entity_type: 'person' | 'org';
  canonical_name: string;
  aliases: string[];
  attributes: { role?: string; emails?: string[] };
  overview: string | null;
  mention_count: number;
  document_count: number;
}

export interface EntityDocMentions {
  document_id: string;
  bates_begin: string;
  title: string | null;
  mentions: { surface_text: string; context_snippet: string | null; start_offset: number | null }[];
}

export interface EntityMentionsPage {
  documents: EntityDocMentions[];
  total: number;
}

export interface EntityConnection {
  entity_id: string;
  canonical_name: string;
  entity_type: 'person' | 'org';
  relationship_type?: string | null;
  description?: string | null;
  document_id?: string | null;
  shared_doc_count?: number | null;
}

export interface SharedEvent {
  event_id: number;
  description: string;
  event_type: string;
  event_date: string | null;
  document_id: string;
}

export interface EntityConnections {
  stated: EntityConnection[];
  cooccurrence: EntityConnection[];
  shared_events: SharedEvent[];
}

export interface EntityListItem {
  id: string;
  entity_type: 'person' | 'org';
  canonical_name: string;
  mention_count: number;
  document_count: number;
}

export interface EntityListPage {
  entities: EntityListItem[];
  total: number;
}

export interface MergeSuggestion {
  id: number;
  score: number;
  rationale: string;
  status: string;
  entity_a: EntityListItem;
  entity_b: EntityListItem;
}

// ── Ontology surfaces ──

export interface TimelineParticipant {
  entity_id: string;
  canonical_name: string;
  entity_type: 'person' | 'org';
}

export type DatePrecision = 'day' | 'month' | 'year' | 'unknown';

export interface TimelineEvent {
  event_id: number;
  description: string;
  event_type: string;
  event_date: string | null;
  date_precision: DatePrecision;
  /** 1 (routine) … 5 (pivotal). Unrated legacy rows present as 3. */
  significance: number;
  /** Verbatim phrase the date was read from; null when undated or unsourced. */
  date_source_text: string | null;
  document_id: string;
  bates_begin: string;
  title: string | null;
  participants: TimelineParticipant[];
}

/** Response of PATCH /api/events/{id} — server-side truth after a date correction. */
export interface EventEditResult {
  event_id: number;
  event_type: string;
  description: string;
  event_date: string | null;
  date_precision: DatePrecision;
  significance: number;
  date_source_text: string | null;
  document_id: string;
}

export interface TimelinePage {
  events: TimelineEvent[];
  total: number;
  undated_count: number;
}

export interface GraphNode {
  id: string;
  canonical_name: string;
  entity_type: 'person' | 'org';
  mention_count: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: 'stated' | 'cooccurrence';
  relationship_type?: string | null;
  weight: number;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
}

export interface ChipEntity {
  entity_id: string;
  canonical_name: string;
  entity_type: 'person' | 'org';
}
