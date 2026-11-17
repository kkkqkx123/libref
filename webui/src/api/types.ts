// Response shapes mirror the backend envelopes in src/libref/web/api.py.
// Refresh the raw snapshot with `npm run gen:api` and keep these in sync.

export interface Envelope<T> {
  items: T[];
  degraded: boolean;
  count: number;
}

export interface SearchItem {
  id: number;
  url: string;
  score: number;
  snippet: string;
  title?: string | null;
  path?: string | null;
  library?: string | null;
  version?: string | null;
  source_group_id?: string;
  chunk_id?: string;
  page_id?: number | null;
  sort_order?: number | null;
  [key: string]: unknown;
}

export interface SearchResponse extends Envelope<SearchItem> {
  reasons: string[];
  answerable: boolean;
  answerability: number;
  suggestions?: Array<Record<string, unknown>>;
  suggest_error?: string | null;
}

export interface Citation {
  url: string;
  title?: string | null;
  id: number;
  library?: string | null;
  version?: string | null;
  source_group_id: string;
  chunk_id: string;
  page_id?: number | null;
  sort_order?: number | null;
}

export interface AnswerResponse {
  answer: string;
  citations: Citation[];
  degraded: boolean;
  reasons: string[];
  model: string;
  confidence: string;
  cited_ids: number[];
}

export interface ChunkLocator {
  source_group_id: string;
  chunk_id: string;
  [key: string]: unknown;
}

export interface ReadResponse {
  url: string;
  content: string;
  library?: string | null;
  version?: string | null;
  title?: string | null;
  path?: string | null;
  source_group_id?: string | null;
  center?: number | null;
  radius?: number | null;
  chunks: number;
  chunk_details: ChunkLocator[];
  docs: ChunkLocator[];
}

export interface LibraryRow {
  id: number;
  ecosystem: string;
  name: string;
  description?: string | null;
  homepage?: string | null;
  docs_url?: string | null;
  repo_url?: string | null;
  versions: number;
  [key: string]: unknown;
}

export interface VersionRow {
  id: number;
  version: string;
  registry_confirmed: boolean;
  created_at?: string | null;
  ecosystem: string;
  pages: number;
  [key: string]: unknown;
}

export interface JobSummary {
  id: number;
  kind: string;
  status: string;
  finished_at?: string | null;
  error?: string | null;
}

export interface JobDetail extends JobSummary {
  result?: unknown;
}

export interface ReviewItem {
  entity_id: number;
  status: string;
  updated_at?: string | null;
  callee?: string | null;
  caller_file?: string | null;
  caller?: string | null;
  confidence?: string | null;
  args?: unknown;
  line_start?: number | null;
  line_end?: number | null;
  [key: string]: unknown;
}

export interface FeedbackItem {
  id: number;
  tenant_id: string;
  entity_type: string;
  entity_id: number;
  kind: string;
  body: string;
  status: string;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface Principal {
  id: number | null;
  name: string;
  kind: string;
  tenant_id: string;
  roles: string[];
  scopes: string[];
}

export type Row = Record<string, unknown>;
