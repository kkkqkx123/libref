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
