"""PostgreSQL consolidated DDL for the shared metadata backend.

The remote branch targets fresh deployments only and never migrates old data,
so the schema is one merged script written in the final shape rather than a
replay of the SQLite migration chain. Every table matches the post-migration
SQLite layout: tenant columns, governance, quota and identity tables are all
included. The FTS5 virtual table plus triggers are replaced by a weighted
tsvector generated column with a GIN index.
"""

from __future__ import annotations

POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS libraries(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ecosystem TEXT NOT NULL DEFAULT 'custom',
  name TEXT NOT NULL,
  description TEXT,
  homepage TEXT,
  docs_url TEXT,
  repo_url TEXT,
  resolved_from TEXT,
  resolved_at TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(ecosystem, name)
);

CREATE TABLE IF NOT EXISTS versions(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version TEXT NOT NULL,
  registry_confirmed INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  site_version TEXT,
  site_version_signal TEXT,
  site_version_sample TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(library_id, version)
);

CREATE TABLE IF NOT EXISTS registry_cache(
  ecosystem TEXT NOT NULL,
  name TEXT NOT NULL,
  docs_url TEXT,
  candidates JSONB NOT NULL DEFAULT '[]',
  versions JSONB NOT NULL DEFAULT '[]',
  stable TEXT,
  homepage TEXT,
  repo_url TEXT,
  fetched_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(ecosystem, name)
);

CREATE TABLE IF NOT EXISTS pages(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  etag TEXT,
  last_modified TEXT,
  fetched_at TEXT,
  content_hash TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, url)
);

CREATE TABLE IF NOT EXISTS documents(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  page_id BIGINT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  content TEXT,
  metadata JSONB,
  sort_order INTEGER NOT NULL,
  title TEXT GENERATED ALWAYS AS (metadata->>'title') STORED,
  path TEXT GENERATED ALWAYS AS (metadata->>'path') STORED,
  content_tsv TSVECTOR GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(metadata->>'title', '')), 'A')
    || setweight(to_tsvector('english', coalesce(content, '')), 'B')
    || setweight(to_tsvector('english',
        regexp_replace(coalesce(metadata->>'path', ''), '[^[:alnum:]_]+', ' ', 'g')), 'C')
    || setweight(to_tsvector('english',
        regexp_replace(coalesce(url, ''), '[^[:alnum:]_]+', ' ', 'g')), 'D')
  ) STORED,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(page_id, sort_order)
);

CREATE INDEX IF NOT EXISTS idx_documents_version ON documents(version_id);
CREATE INDEX IF NOT EXISTS idx_documents_page ON documents(page_id);
CREATE INDEX IF NOT EXISTS idx_documents_tsv ON documents USING GIN(content_tsv);
CREATE INDEX IF NOT EXISTS idx_documents_title_trgm ON documents(title text_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_pages_version ON pages(version_id);
CREATE INDEX IF NOT EXISTS idx_versions_library ON versions(library_id);

CREATE TABLE IF NOT EXISTS api_entities(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_uid TEXT NOT NULL UNIQUE,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  qualified_name TEXT NOT NULL,
  kind TEXT NOT NULL,
  parent TEXT,
  signature TEXT NOT NULL DEFAULT '',
  params JSONB NOT NULL DEFAULT '[]',
  bases JSONB NOT NULL DEFAULT '[]',
  start_byte INTEGER NOT NULL DEFAULT 0,
  end_byte INTEGER NOT NULL DEFAULT 0,
  depth INTEGER NOT NULL DEFAULT 0,
  file TEXT NOT NULL DEFAULT '',
  line_start INTEGER NOT NULL DEFAULT 1,
  line_end INTEGER NOT NULL DEFAULT 1,
  param_types JSONB NOT NULL DEFAULT '[]',
  return_type TEXT NOT NULL DEFAULT '',
  has_doc INTEGER NOT NULL DEFAULT 0,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, qualified_name, kind)
);

CREATE INDEX IF NOT EXISTS idx_api_entities_name ON api_entities(qualified_name);
CREATE INDEX IF NOT EXISTS idx_api_entities_file ON api_entities(version_id, file);

CREATE TABLE IF NOT EXISTS call_instances(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  call_uid TEXT NOT NULL UNIQUE,
  callee_entity_id BIGINT REFERENCES api_entities(id) ON DELETE SET NULL,
  callee_name TEXT NOT NULL,
  args JSONB NOT NULL DEFAULT '[]',
  repo TEXT NOT NULL,
  caller_file TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED','INFERRED','AMBIGUOUS')),
  library_id BIGINT REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT REFERENCES versions(id) ON DELETE CASCADE,
  caller TEXT,
  commit_sha TEXT NOT NULL DEFAULT '',
  receiver TEXT,
  func TEXT NOT NULL DEFAULT '',
  argument_count INTEGER NOT NULL DEFAULT 0,
  call_order INTEGER NOT NULL DEFAULT 0,
  start_byte INTEGER NOT NULL DEFAULT 0,
  end_byte INTEGER NOT NULL DEFAULT 0,
  scope TEXT,
  resolve_kind TEXT NOT NULL DEFAULT '',
  caller_entity_id BIGINT REFERENCES api_entities(id) ON DELETE SET NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_call_instances_callee ON call_instances(callee_entity_id);
CREATE INDEX IF NOT EXISTS idx_call_instances_version ON call_instances(version_id, caller_file);
CREATE INDEX IF NOT EXISTS idx_call_instances_repo ON call_instances(repo, caller_file);
CREATE INDEX IF NOT EXISTS idx_call_instances_version_callee ON call_instances(version_id, callee_name);
CREATE INDEX IF NOT EXISTS idx_call_instances_scope ON call_instances(version_id, caller_file, call_order, start_byte);
CREATE INDEX IF NOT EXISTS idx_call_instances_scope_key ON call_instances(version_id, scope);
CREATE INDEX IF NOT EXISTS idx_call_instances_caller ON call_instances(caller_entity_id);
CREATE INDEX IF NOT EXISTS idx_call_instances_resolve ON call_instances(version_id, resolve_kind);

CREATE TABLE IF NOT EXISTS seq_patterns(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  pattern JSONB NOT NULL,
  support INTEGER NOT NULL,
  is_closed INTEGER NOT NULL DEFAULT 0,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, pattern)
);

CREATE INDEX IF NOT EXISTS idx_seq_patterns_support ON seq_patterns(version_id, support DESC);

CREATE TABLE IF NOT EXISTS review_status(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','approved','conflict')),
  updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_review_pending
  ON review_status(entity_type, status, updated_at, entity_id);

CREATE TABLE IF NOT EXISTS schema_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vec_version_meta(
  version_id BIGINT PRIMARY KEY REFERENCES versions(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS repos(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  url TEXT,
  commit_sha TEXT,
  local_path TEXT NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS ast_cache(
  repo TEXT NOT NULL,
  path TEXT NOT NULL,
  file_sha1 TEXT NOT NULL,
  fp TEXT NOT NULL,
  cfg_version INTEGER NOT NULL,
  records JSONB NOT NULL DEFAULT '[]',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  PRIMARY KEY(repo, path, fp, cfg_version)
);

CREATE TABLE IF NOT EXISTS feature_nodes(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  parent TEXT NOT NULL DEFAULT '',
  depth INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'package',
  calls INTEGER NOT NULL,
  scopes INTEGER NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, path)
);

CREATE INDEX IF NOT EXISTS idx_feature_nodes_parent ON feature_nodes(version_id, parent);
CREATE INDEX IF NOT EXISTS idx_feature_nodes_calls ON feature_nodes(version_id, calls DESC);

CREATE TABLE IF NOT EXISTS api_clusters(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  cluster_id INTEGER NOT NULL,
  member TEXT NOT NULL,
  size INTEGER NOT NULL,
  representative TEXT NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, member)
);

CREATE INDEX IF NOT EXISTS idx_api_clusters_version ON api_clusters(version_id, cluster_id);

CREATE TABLE IF NOT EXISTS pattern_labels(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  pattern_hash TEXT NOT NULL,
  pattern JSONB NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL DEFAULT 'v1',
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  category_path JSONB NOT NULL DEFAULT '[]',
  workflow TEXT NOT NULL DEFAULT '',
  related_apis JSONB NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, pattern_hash, model, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_pattern_labels_version ON pattern_labels(version_id);

CREATE TABLE IF NOT EXISTS discovery_cache(
  ecosystem TEXT NOT NULL,
  query TEXT NOT NULL,
  candidates JSONB NOT NULL DEFAULT '[]',
  fetched_at DOUBLE PRECISION NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  PRIMARY KEY(ecosystem, query)
);

CREATE TABLE IF NOT EXISTS repo_dependencies(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  ecosystem TEXT NOT NULL,
  name TEXT NOT NULL,
  req TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, ecosystem, name, source_file)
);

CREATE INDEX IF NOT EXISTS idx_repo_dependencies_version ON repo_dependencies(version_id);

CREATE TABLE IF NOT EXISTS import_edges(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  importer_file TEXT NOT NULL,
  alias TEXT NOT NULL,
  target TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'import',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, importer_file, alias)
);

CREATE INDEX IF NOT EXISTS idx_import_edges_target ON import_edges(version_id, target);
CREATE INDEX IF NOT EXISTS idx_import_edges_file ON import_edges(version_id, importer_file);

CREATE TABLE IF NOT EXISTS doc_link_edges(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_id BIGINT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  src_url TEXT NOT NULL,
  dst_url TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'internal',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, src_url, dst_url)
);

CREATE INDEX IF NOT EXISTS idx_doc_link_edges_dst ON doc_link_edges(version_id, dst_url);
CREATE INDEX IF NOT EXISTS idx_doc_link_edges_src ON doc_link_edges(version_id, src_url);

CREATE TABLE IF NOT EXISTS ingest_progress(
  version_id BIGINT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  total INTEGER NOT NULL DEFAULT 0,
  done INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT NOT NULL DEFAULT '',
  batch_size INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  vector_done INTEGER NOT NULL DEFAULT 0,
  pending TEXT NOT NULL DEFAULT '[]',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  PRIMARY KEY(version_id, kind)
);

CREATE TABLE IF NOT EXISTS jobs(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind TEXT NOT NULL,
  params JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','running','done','failed')),
  error TEXT,
  heartbeat_at DOUBLE PRECISION,
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  result JSONB NOT NULL DEFAULT '{}',
  finished_at TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id BIGINT,
  owner TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_recovery ON jobs(status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant ON jobs(tenant_id, status);

CREATE TABLE IF NOT EXISTS robots_cache(
  host TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  body_md5 TEXT NOT NULL,
  fetched_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS deletion_log(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library TEXT NOT NULL,
  version TEXT NOT NULL,
  ecosystem TEXT NOT NULL DEFAULT 'custom',
  reason TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id BIGINT
);

CREATE TABLE IF NOT EXISTS version_locks(
  version_key TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  heartbeat_at DOUBLE PRECISION NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS principals(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  kind TEXT NOT NULL DEFAULT 'human',
  name TEXT NOT NULL,
  roles JSONB NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  UNIQUE(tenant_id, name)
);

CREATE TABLE IF NOT EXISTS api_keys(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  principal_id BIGINT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL,
  scopes JSONB NOT NULL DEFAULT '[]',
  expires_at DOUBLE PRECISION,
  revoked_at DOUBLE PRECISION,
  last_used_at DOUBLE PRECISION,
  created_by BIGINT,
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);

CREATE TABLE IF NOT EXISTS feedback(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id BIGINT,
  entity_type TEXT NOT NULL DEFAULT 'call_instance',
  entity_id BIGINT NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'general',
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open'
      CHECK (status IN ('open','acknowledged','resolved')),
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  resolved_by BIGINT,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id, status, id);
CREATE INDEX IF NOT EXISTS idx_feedback_resolve ON feedback(status, resolved_at);

CREATE TABLE IF NOT EXISTS index_requests(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id BIGINT,
  requester TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK (kind IN ('repo','docs')),
  target TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  params JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','approved','queued','running','done','failed',
                        'rejected','cancelled')),
  dedupe_key TEXT NOT NULL,
  approved_by BIGINT,
  reject_reason TEXT NOT NULL DEFAULT '',
  job_id BIGINT,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE INDEX IF NOT EXISTS idx_index_requests_tenant_owner
  ON index_requests(tenant_id, principal_id, id);
CREATE INDEX IF NOT EXISTS idx_index_requests_status
  ON index_requests(tenant_id, status, id);
CREATE INDEX IF NOT EXISTS idx_index_requests_dedupe
  ON index_requests(tenant_id, dedupe_key, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_index_requests_active_dedupe
  ON index_requests(tenant_id, dedupe_key)
  WHERE status IN ('pending','approved','queued','running');

CREATE TABLE IF NOT EXISTS audit_log(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id BIGINT,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT 'ok',
  request_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id, id);

CREATE TABLE IF NOT EXISTS quota_policies(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id BIGINT,
  resource TEXT NOT NULL,
  limit_value INTEGER NOT NULL,
  window_type TEXT NOT NULL DEFAULT 'hour'
      CHECK (window_type IN ('hour','day','month')),
  updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  UNIQUE(tenant_id, principal_id, resource, window_type)
);

CREATE INDEX IF NOT EXISTS idx_quota_policies_subject
  ON quota_policies(tenant_id, principal_id, resource);

CREATE TABLE IF NOT EXISTS usage_counters(
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id BIGINT,
  resource TEXT NOT NULL,
  period_key TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  PRIMARY KEY(tenant_id, principal_id, resource, period_key)
);

CREATE TABLE IF NOT EXISTS quota_events(
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id BIGINT,
  resource TEXT NOT NULL,
  result TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  limit_value INTEGER NOT NULL DEFAULT 0,
  request_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE INDEX IF NOT EXISTS idx_quota_events_subject
  ON quota_events(tenant_id, principal_id, created_at);

CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url);
CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url);
CREATE INDEX IF NOT EXISTS idx_libraries_tenant ON libraries(tenant_id, ecosystem, name);
CREATE INDEX IF NOT EXISTS idx_versions_tenant ON versions(tenant_id, library_id);
"""
