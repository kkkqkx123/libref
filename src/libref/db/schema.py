"""DDL definitions: core tables, full-text index and triggers, extension tables, vector metadata, content fingerprints."""

from __future__ import annotations

# ── Core tables with full-text index and triggers ──
INITIAL_SCHEMA_SQL = """
CREATE TABLE libraries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ecosystem TEXT NOT NULL DEFAULT 'custom',
  name TEXT NOT NULL,
  description TEXT,
  homepage TEXT,
  docs_url TEXT,
  repo_url TEXT,
  resolved_from TEXT,
  resolved_at TEXT,
  UNIQUE(ecosystem, name)
);

CREATE TABLE versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version TEXT NOT NULL,
  registry_confirmed INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(library_id, version)
);

CREATE TABLE registry_cache(
  ecosystem TEXT NOT NULL,
  name TEXT NOT NULL,
  docs_url TEXT,
  candidates JSON NOT NULL DEFAULT '[]',
  versions JSON NOT NULL DEFAULT '[]',
  stable TEXT,
  homepage TEXT,
  repo_url TEXT,
  fetched_at REAL NOT NULL,
  PRIMARY KEY(ecosystem, name)
);

CREATE TABLE pages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  etag TEXT,
  last_modified TEXT,
  fetched_at TEXT,
  UNIQUE(version_id, url)
);

CREATE TABLE documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  content TEXT,
  metadata JSON,
  sort_order INTEGER NOT NULL,
  title TEXT GENERATED ALWAYS AS (json_extract(metadata, '$.title')) STORED,
  path TEXT GENERATED ALWAYS AS (json_extract(metadata, '$.path')) STORED,
  UNIQUE(page_id, sort_order)
);

CREATE INDEX idx_documents_version ON documents(version_id);
CREATE INDEX idx_documents_page ON documents(page_id);
CREATE INDEX idx_pages_version ON pages(version_id);
CREATE INDEX idx_versions_library ON versions(library_id);

-- External-content index over documents. Title and path are read-only
-- generated columns derived from metadata JSON, so every index column
-- exists in the content table and native maintenance works. Index writes
-- must go through the triggers below or the maintenance rebuild; partial
-- out-of-band writes leave terms out of agreement and later triggered
-- deletes fail with a corruption error.
CREATE VIRTUAL TABLE documents_fts USING fts5(
  content,
  title,
  url,
  path,
  tokenize='porter unicode61',
  content='documents',
  content_rowid='id'
);

CREATE TRIGGER documents_fts_after_insert
AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, content, title, url, path)
  VALUES(new.id, new.content, new.title, new.url, new.path);
END;

CREATE TRIGGER documents_fts_after_delete
AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, content, title, url, path)
  VALUES('delete', old.id, old.content, old.title, old.url, old.path);
END;

CREATE TRIGGER documents_fts_after_update
AFTER UPDATE OF content, metadata, url, page_id ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, content, title, url, path)
  VALUES('delete', old.id, old.content, old.title, old.url, old.path);
  INSERT INTO documents_fts(rowid, content, title, url, path)
  VALUES(new.id, new.content, new.title, new.url, new.path);
END;
"""

# ── Extension tables: source mining and review ──
MINING_REVIEW_SCHEMA_SQL = """
-- api_entities: deterministic identity plus qualified name plus library/version ownership
CREATE TABLE api_entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_uid TEXT NOT NULL UNIQUE,          -- semantic-key hash; equal means the same entity
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  qualified_name TEXT NOT NULL,
  kind TEXT NOT NULL,                       -- class / function / method
  UNIQUE(version_id, qualified_name, kind)
);

CREATE INDEX idx_api_entities_name ON api_entities(qualified_name);

-- call_instances: call instance records, owned by library version; repo name is for provenance only
CREATE TABLE call_instances(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_uid TEXT NOT NULL UNIQUE,            -- semantic-key hash; equal means the same call
  callee_entity_id INTEGER REFERENCES api_entities(id) ON DELETE SET NULL,
  callee_name TEXT NOT NULL,                -- qualified name after alias resolution
  args JSON NOT NULL DEFAULT '[]',          -- list of argument summaries
  repo TEXT NOT NULL,
  caller_file TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED','INFERRED','AMBIGUOUS'))
);

CREATE INDEX idx_call_instances_callee ON call_instances(callee_entity_id);

-- seq_patterns: sequence mining output
CREATE TABLE seq_patterns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  pattern JSON NOT NULL,                    -- sequence of qualified names
  support INTEGER NOT NULL,
  is_closed INTEGER NOT NULL DEFAULT 0,
  UNIQUE(version_id, pattern)
);

CREATE INDEX idx_seq_patterns_support ON seq_patterns(version_id, support DESC);

-- review_status: review workflow
CREATE TABLE review_status(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,                -- document / call_instance / seq_pattern
  entity_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','approved','conflict')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(entity_type, entity_id)
);
"""

# ── Vector index metadata: vectors live in an external vector service; only model name and dimension are stored here ──
VECTOR_META_SQL = """
CREATE TABLE schema_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# ── Page content fingerprint: unchanged content short-circuits to the unchanged outcome ──
PAGES_CONTENT_HASH_SQL = """
ALTER TABLE pages ADD COLUMN content_hash TEXT;
"""

# ── Repo registration and incremental cache, plus backfilling call-instance version ownership ──
REPOS_ASTCACHE_SQL = """
CREATE TABLE repos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  url TEXT,
  commit_sha TEXT,
  local_path TEXT NOT NULL
);

CREATE TABLE ast_cache(
  repo TEXT NOT NULL,
  path TEXT NOT NULL,
  file_sha1 TEXT NOT NULL,
  fp TEXT NOT NULL,
  cfg_version INTEGER NOT NULL,
  records JSON NOT NULL DEFAULT '[]',
  PRIMARY KEY(repo, path, fp, cfg_version)
);

ALTER TABLE call_instances ADD COLUMN library_id INTEGER
  REFERENCES libraries(id) ON DELETE CASCADE;
ALTER TABLE call_instances ADD COLUMN version_id INTEGER
  REFERENCES versions(id) ON DELETE CASCADE;
ALTER TABLE call_instances ADD COLUMN caller TEXT;

CREATE INDEX idx_call_instances_version ON call_instances(version_id, caller_file);
CREATE INDEX idx_call_instances_repo ON call_instances(repo, caller_file);
"""

# ── Mining provenance and query support: commit identity plus common per-version filter indexes ──
MINING_PROVENANCE_INDEX_SQL = """
ALTER TABLE call_instances ADD COLUMN commit_sha TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_call_instances_version_callee ON call_instances(version_id, callee_name);
CREATE INDEX idx_call_instances_scope ON call_instances(version_id, caller_file, call_order, start_byte);
"""

# ── Fields needed by chunking and relations: declaration-signature family and call-split family ──
AST_CHUNK_RELATION_SQL = """
ALTER TABLE api_entities ADD COLUMN parent TEXT;
ALTER TABLE api_entities ADD COLUMN signature TEXT NOT NULL DEFAULT '';
ALTER TABLE api_entities ADD COLUMN params JSON NOT NULL DEFAULT '[]';
ALTER TABLE api_entities ADD COLUMN bases JSON NOT NULL DEFAULT '[]';
ALTER TABLE api_entities ADD COLUMN start_byte INTEGER NOT NULL DEFAULT 0;
ALTER TABLE api_entities ADD COLUMN end_byte INTEGER NOT NULL DEFAULT 0;
ALTER TABLE api_entities ADD COLUMN depth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_instances ADD COLUMN receiver TEXT;
ALTER TABLE call_instances ADD COLUMN func TEXT NOT NULL DEFAULT '';
ALTER TABLE call_instances ADD COLUMN argument_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_instances ADD COLUMN call_order INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_instances ADD COLUMN start_byte INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_instances ADD COLUMN end_byte INTEGER NOT NULL DEFAULT 0;
"""

# ── Per-version vector embedding records: model drift is repaired per version, collection rebuild only on dimension change ──
VEC_VERSION_META_SQL = """
CREATE TABLE vec_version_meta(
  version_id INTEGER PRIMARY KEY REFERENCES versions(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ── Operations governance tables: jobs, cache, audit trail, version mutex; all created idempotently ──
GOVERNANCE_SQL = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  params JSON NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','running','done','failed')),
  error TEXT,
  heartbeat_at REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  result JSON NOT NULL DEFAULT '{}',
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS robots_cache(
  host TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  body_md5 TEXT NOT NULL,
  fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deletion_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library TEXT NOT NULL,
  version TEXT NOT NULL,
  ecosystem TEXT NOT NULL DEFAULT 'custom',
  reason TEXT NOT NULL,
  detail JSON NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS version_locks(
  version_key TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  heartbeat_at REAL NOT NULL
);
"""
# ── Hierarchical features and API grouping: built in batch, browsed at query time ──
FEATURE_TREE_CLUSTER_SQL = """
CREATE TABLE feature_nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  parent TEXT NOT NULL DEFAULT '',
  depth INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'package',
  calls INTEGER NOT NULL,
  scopes INTEGER NOT NULL,
  UNIQUE(version_id, path)
);
CREATE INDEX idx_feature_nodes_parent ON feature_nodes(version_id, parent);
CREATE INDEX idx_feature_nodes_calls ON feature_nodes(version_id, calls DESC);
CREATE TABLE api_clusters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  cluster_id INTEGER NOT NULL,
  member TEXT NOT NULL,
  size INTEGER NOT NULL,
  representative TEXT NOT NULL,
  UNIQUE(version_id, member)
);
CREATE INDEX idx_api_clusters_version ON api_clusters(version_id, cluster_id);
"""

# ── Index requests: approval-gated doc and repository indexing ──
INDEX_REQUESTS_SQL = """
CREATE TABLE IF NOT EXISTS index_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id INTEGER,
  requester TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK (kind IN ('repo','docs')),
  target TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  params JSON NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','approved','queued','running','done','failed',
                        'rejected','cancelled')),
  dedupe_key TEXT NOT NULL,
  approved_by INTEGER,
  reject_reason TEXT NOT NULL DEFAULT '',
  job_id INTEGER,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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
"""

# ── Usage corpora and mining profiles: source-aware API usage mining ──
USAGE_CORPUS_SQL = """
CREATE TABLE IF NOT EXISTS code_sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  source_key TEXT NOT NULL,
  source_role TEXT NOT NULL,
  origin_kind TEXT NOT NULL,
  source_url TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '',
  selection JSON NOT NULL DEFAULT '{}',
  target_filter JSON NOT NULL DEFAULT '{}',
  weight REAL NOT NULL DEFAULT 1.0,
  limits JSON NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, source_key)
);
CREATE INDEX IF NOT EXISTS idx_code_sources_version ON code_sources(version_id, enabled);

CREATE TABLE IF NOT EXISTS mining_profiles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  source_ids JSON NOT NULL DEFAULT '[]',
  support_mode TEXT NOT NULL DEFAULT 'sample',
  params JSON NOT NULL DEFAULT '{}',
  fingerprint TEXT NOT NULL DEFAULT '',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(version_id, name)
);

ALTER TABLE call_instances ADD COLUMN source_id INTEGER;
ALTER TABLE call_instances ADD COLUMN sample_id TEXT NOT NULL DEFAULT '';
ALTER TABLE call_instances ADD COLUMN source_role TEXT NOT NULL DEFAULT 'implementation';
ALTER TABLE call_instances ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'repo_file';
ALTER TABLE call_instances ADD COLUMN in_target_api INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_instances ADD COLUMN target_status TEXT NOT NULL DEFAULT 'current';
CREATE INDEX IF NOT EXISTS idx_call_instances_source ON call_instances(version_id, source_id);
CREATE INDEX IF NOT EXISTS idx_call_instances_sample ON call_instances(version_id, sample_id);
CREATE INDEX IF NOT EXISTS idx_call_instances_role ON call_instances(version_id, source_role);

DROP TABLE IF EXISTS seq_patterns;
CREATE TABLE seq_patterns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  profile_id INTEGER NOT NULL,
  pattern JSON NOT NULL,
  support INTEGER NOT NULL,
  is_closed INTEGER NOT NULL DEFAULT 0,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(profile_id, pattern)
);
CREATE INDEX idx_seq_patterns_support ON seq_patterns(version_id, profile_id, support DESC);

DROP TABLE IF EXISTS feature_nodes;
CREATE TABLE feature_nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  profile_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  parent TEXT NOT NULL DEFAULT '',
  depth INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'package',
  calls INTEGER NOT NULL,
  scopes INTEGER NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(profile_id, path)
);
CREATE INDEX idx_feature_nodes_parent ON feature_nodes(version_id, profile_id, parent);
CREATE INDEX idx_feature_nodes_calls ON feature_nodes(version_id, profile_id, calls DESC);

DROP TABLE IF EXISTS api_clusters;
CREATE TABLE api_clusters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  profile_id INTEGER NOT NULL,
  cluster_id INTEGER NOT NULL,
  member TEXT NOT NULL,
  size INTEGER NOT NULL,
  representative TEXT NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(profile_id, member)
);
CREATE INDEX idx_api_clusters_version ON api_clusters(version_id, profile_id, cluster_id);

DROP TABLE IF EXISTS pattern_labels;
CREATE TABLE pattern_labels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  profile_id INTEGER NOT NULL,
  pattern_hash TEXT NOT NULL,
  pattern JSON NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL DEFAULT 'v1',
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  category_path JSON NOT NULL DEFAULT '[]',
  workflow TEXT NOT NULL DEFAULT '',
  related_apis JSON NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(profile_id, pattern_hash, model, prompt_version)
);
CREATE INDEX idx_pattern_labels_version ON pattern_labels(version_id, profile_id);
"""

# ── Query support: url lookups, review state scan, job recovery, embeddable filter ──
QUERY_SUPPORT_SQL = """
CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url);
CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url);
CREATE INDEX IF NOT EXISTS idx_documents_group
  ON documents(url, json_extract(metadata, '$.source_group_id'));
CREATE INDEX IF NOT EXISTS idx_documents_chunk_path
  ON documents(version_id, json_extract(metadata, '$.chunk_path'));
CREATE INDEX IF NOT EXISTS idx_review_pending
  ON review_status(entity_type, status, updated_at, entity_id);
CREATE INDEX IF NOT EXISTS idx_jobs_recovery ON jobs(status, heartbeat_at);
"""

# ── Call scope key: persisted grouping identity so reads never recompute it per row ──
CALL_SCOPE_SQL = """
ALTER TABLE call_instances ADD COLUMN scope TEXT;
UPDATE call_instances SET scope = caller_file || char(0) || COALESCE(caller, '')
  WHERE scope IS NULL;
CREATE INDEX IF NOT EXISTS idx_call_instances_scope_key ON call_instances(version_id, scope);
"""

# ── Discovery cache: ranked fuzzy-search conclusions, rebuildable from registries ──
DISCOVERY_CACHE_SQL = """
CREATE TABLE discovery_cache(
  ecosystem TEXT NOT NULL,
  query TEXT NOT NULL,
  candidates JSON NOT NULL DEFAULT '[]',
  fetched_at REAL NOT NULL,
  PRIMARY KEY(ecosystem, query)
);
"""

# ── Manifest-declared repo dependencies: version-scoped edges, rebuilt on every index ──
REPO_DEPS_SQL = """
CREATE TABLE repo_dependencies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  ecosystem TEXT NOT NULL,
  name TEXT NOT NULL,
  req TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  UNIQUE(version_id, ecosystem, name, source_file)
);
CREATE INDEX IF NOT EXISTS idx_repo_dependencies_version ON repo_dependencies(version_id);
"""

# ── Pattern-level semantic labels: rebuildable model output, never mixed with static facts ──
PATTERN_LABEL_SQL = """
CREATE TABLE pattern_labels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  pattern_hash TEXT NOT NULL,
  pattern JSON NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL DEFAULT 'v1',
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  category_path JSON NOT NULL DEFAULT '[]',
  workflow TEXT NOT NULL DEFAULT '',
  related_apis JSON NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(version_id, pattern_hash, model, prompt_version)
);
CREATE INDEX idx_pattern_labels_version ON pattern_labels(version_id);
"""

# ── Usage facet rows: per-dimension aggregated call shapes, rebuilt per profile ──
FACET_DERIVED_SQL = """
CREATE TABLE IF NOT EXISTS feature_facets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  profile_id INTEGER NOT NULL,
  facet TEXT NOT NULL,
  facet_key TEXT NOT NULL,
  value TEXT NOT NULL,
  calls INTEGER NOT NULL,
  scopes INTEGER NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(profile_id, facet_key)
);
CREATE INDEX IF NOT EXISTS idx_feature_facets_lookup
  ON feature_facets(version_id, profile_id, facet, calls DESC);
"""

# ── Group-level semantic labels: rebuildable model output for clusters ──
CLUSTER_LABEL_SQL = """
CREATE TABLE IF NOT EXISTS cluster_labels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  profile_id INTEGER NOT NULL,
  cluster_key TEXT NOT NULL,
  members JSON NOT NULL DEFAULT '[]',
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL DEFAULT 'v1',
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  category_path JSON NOT NULL DEFAULT '[]',
  workflow TEXT NOT NULL DEFAULT '',
  related_apis JSON NOT NULL DEFAULT '[]',
  source_note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  tenant_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE(profile_id, cluster_key, model, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_cluster_labels_version
  ON cluster_labels(version_id, profile_id);
"""

# ── Ingest progress cursors: one row per version and kind, resume offset plus list fingerprint ──
PROGRESS_SQL = """
CREATE TABLE IF NOT EXISTS ingest_progress(
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  total INTEGER NOT NULL DEFAULT 0,
  done INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT NOT NULL DEFAULT '',
  batch_size INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(version_id, kind)
);
"""

# ── Symbol detail: file location plus semantic columns already extracted upstream ──
SYMBOL_DETAIL_SQL = """
ALTER TABLE api_entities ADD COLUMN file TEXT NOT NULL DEFAULT '';
ALTER TABLE api_entities ADD COLUMN line_start INTEGER NOT NULL DEFAULT 1;
ALTER TABLE api_entities ADD COLUMN line_end INTEGER NOT NULL DEFAULT 1;
ALTER TABLE api_entities ADD COLUMN param_types JSON NOT NULL DEFAULT '[]';
ALTER TABLE api_entities ADD COLUMN return_type TEXT NOT NULL DEFAULT '';
ALTER TABLE api_entities ADD COLUMN has_doc INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_api_entities_file ON api_entities(version_id, file);
"""

# ── Call resolution provenance: how each call was resolved plus caller linkage ──
CALL_RESOLVE_SQL = """
ALTER TABLE call_instances ADD COLUMN resolve_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE call_instances ADD COLUMN caller_entity_id INTEGER REFERENCES api_entities(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_call_instances_caller ON call_instances(caller_entity_id);
CREATE INDEX IF NOT EXISTS idx_call_instances_resolve ON call_instances(version_id, resolve_kind);
"""

# ── File import edges: alias targets observed by the parser, rebuilt on every index ──
IMPORT_EDGES_SQL = """
CREATE TABLE import_edges(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  importer_file TEXT NOT NULL,
  alias TEXT NOT NULL,
  target TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'import',
  UNIQUE(version_id, importer_file, alias)
);
CREATE INDEX IF NOT EXISTS idx_import_edges_target ON import_edges(version_id, target);
CREATE INDEX IF NOT EXISTS idx_import_edges_file ON import_edges(version_id, importer_file);
"""

# ── Document link edges: per-version source page to target page linkage ──
DOC_LINK_EDGES_SQL = """
CREATE TABLE doc_link_edges(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  src_url TEXT NOT NULL,
  dst_url TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'internal',
  UNIQUE(version_id, src_url, dst_url)
);
CREATE INDEX IF NOT EXISTS idx_doc_link_edges_dst ON doc_link_edges(version_id, dst_url);
CREATE INDEX IF NOT EXISTS idx_doc_link_edges_src ON doc_link_edges(version_id, src_url);
"""

# ── Vector sync watermark: records how far vector sync progressed per version ──
PROGRESS_VECTOR_WATERMARK_SQL = """
ALTER TABLE ingest_progress ADD COLUMN vector_done INTEGER NOT NULL DEFAULT 0;
UPDATE ingest_progress SET vector_done = done WHERE vector_done = 0;
"""

# ── Site-stated documentation version: what the site says it documents ──
VERSION_SITE_EVIDENCE_SQL = """
ALTER TABLE versions ADD COLUMN site_version TEXT;
ALTER TABLE versions ADD COLUMN site_version_signal TEXT;
ALTER TABLE versions ADD COLUMN site_version_sample TEXT;
"""

# ── Follow-queue persistence: the link-followed URLs outstanding at failure ──
PROGRESS_PENDING_SQL = """
ALTER TABLE ingest_progress ADD COLUMN pending TEXT NOT NULL DEFAULT '[]';
"""

# ── Multi-tenant identity and isolation: shared library with tenant filtering ──
_IDENTITY_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS principals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  kind TEXT NOT NULL DEFAULT 'human',
  name TEXT NOT NULL,
  roles JSON NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(tenant_id, name)
);
CREATE TABLE IF NOT EXISTS api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL,
  scopes JSON NOT NULL DEFAULT '[]',
  expires_at REAL,
  revoked_at REAL,
  last_used_at REAL,
  created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id INTEGER,
  entity_type TEXT NOT NULL DEFAULT 'call_instance',
  entity_id INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'general',
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open'
      CHECK (status IN ('open','acknowledged','resolved')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_by INTEGER,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id, status, id);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id INTEGER,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT 'ok',
  request_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id, id);
"""

MULTI_TENANT_SQL = (
    _IDENTITY_TABLES_SQL
    + """
ALTER TABLE libraries ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE versions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE pages ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE documents ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE api_entities ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE call_instances ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE seq_patterns ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE review_status ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE repos ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE ast_cache ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE vec_version_meta ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE feature_nodes ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE api_clusters ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE discovery_cache ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE repo_dependencies ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE pattern_labels ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE ingest_progress ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE import_edges ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE doc_link_edges ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE jobs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE jobs ADD COLUMN principal_id INTEGER;
ALTER TABLE jobs ADD COLUMN owner TEXT;
ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deletion_log ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE deletion_log ADD COLUMN principal_id INTEGER;
ALTER TABLE version_locks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_libraries_tenant ON libraries(tenant_id, ecosystem, name);
CREATE INDEX IF NOT EXISTS idx_versions_tenant ON versions(tenant_id, library_id);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant ON jobs(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_feedback_resolve ON feedback(status, resolved_at);
"""
)

# ── Identity tables alone for runtime ensure without column alters ──
AUTH_TABLES_SQL = _IDENTITY_TABLES_SQL

# ── Per-principal quota governance: policies, aggregated usage, deny/degrade events ──
QUOTA_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS quota_policies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id INTEGER,                     -- NULL = tenant default policy
  resource TEXT NOT NULL,                   -- http_requests / llm_prompt_tokens / ...
  limit_value INTEGER NOT NULL,             -- 0 = unlimited
  window_type TEXT NOT NULL DEFAULT 'hour'  -- hour / day / month
      CHECK (window_type IN ('hour','day','month')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(tenant_id, principal_id, resource, window_type)
);
CREATE INDEX IF NOT EXISTS idx_quota_policies_subject
  ON quota_policies(tenant_id, principal_id, resource);

CREATE TABLE IF NOT EXISTS usage_counters(
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id INTEGER,
  resource TEXT NOT NULL,
  period_key TEXT NOT NULL,                 -- e.g. 2026-11-15T14 (hour bucket, UTC)
  used INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant_id, principal_id, resource, period_key)
);

CREATE TABLE IF NOT EXISTS quota_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  principal_id INTEGER,
  resource TEXT NOT NULL,
  result TEXT NOT NULL,                     -- denied / degraded
  used INTEGER NOT NULL DEFAULT 0,
  limit_value INTEGER NOT NULL DEFAULT 0,
  request_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quota_events_subject
  ON quota_events(tenant_id, principal_id, created_at);
"""

# ── Quota tables alone for runtime ensure without touching other groups ──
QUOTA_RUNTIME_SQL = QUOTA_TABLES_SQL

# ── Call relation shape: independent from resolution provenance ──
CALL_RELATION_SQL = """
ALTER TABLE call_instances ADD COLUMN relation_kind TEXT NOT NULL DEFAULT 'direct';
CREATE INDEX IF NOT EXISTS idx_call_instances_relation
  ON call_instances(version_id, relation_kind);
"""

# ── Macro body evidence: definition attribution plus target macro identity ──
MACRO_EVIDENCE_SQL = """
ALTER TABLE call_instances ADD COLUMN macro_generated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_instances ADD COLUMN macro_host TEXT NOT NULL DEFAULT '';
ALTER TABLE call_instances ADD COLUMN macro_host_file TEXT NOT NULL DEFAULT '';
ALTER TABLE call_instances ADD COLUMN macro_definition_line INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_instances ADD COLUMN macro_nesting INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_instances ADD COLUMN macro_conditions JSON NOT NULL DEFAULT '[]';
ALTER TABLE call_instances ADD COLUMN macro_body_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE call_instances ADD COLUMN macro_body_signature TEXT NOT NULL DEFAULT '';
ALTER TABLE api_entities ADD COLUMN macro_body_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE api_entities ADD COLUMN macro_body_signature TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_call_instances_macro_host
  ON call_instances(version_id, macro_host);
"""
