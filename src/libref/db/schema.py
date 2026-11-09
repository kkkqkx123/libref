"""DDL 定义：核心表、全文索引与触发器、扩展表、向量元数据、内容指纹。"""

from __future__ import annotations

# ── 核心表加全文索引与触发器 ──
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
  UNIQUE(page_id, sort_order)
);

CREATE INDEX idx_documents_version ON documents(version_id);
CREATE INDEX idx_documents_page ON documents(page_id);
CREATE INDEX idx_pages_version ON pages(version_id);
CREATE INDEX idx_versions_library ON versions(library_id);

CREATE VIRTUAL TABLE documents_fts USING fts5(
  content,
  title,
  url,
  path,
  tokenize='porter unicode61',
  content='documents',
  content_rowid='id'
);

-- title 与 path 存于 documents.metadata JSON，触发器同步抽取
CREATE TRIGGER documents_fts_after_insert
AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, content, title, url, path)
  VALUES(new.id, new.content, json_extract(new.metadata, '$.title'),
         new.url, json_extract(new.metadata, '$.path'));
END;

CREATE TRIGGER documents_fts_after_delete
AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, content, title, url, path)
  VALUES('delete', old.id, old.content, json_extract(old.metadata, '$.title'),
         old.url, json_extract(old.metadata, '$.path'));
END;

CREATE TRIGGER documents_fts_after_update
AFTER UPDATE OF content, metadata, url, page_id ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, content, title, url, path)
  VALUES('delete', old.id, old.content, json_extract(old.metadata, '$.title'),
         old.url, json_extract(old.metadata, '$.path'));
  INSERT INTO documents_fts(rowid, content, title, url, path)
  VALUES(new.id, new.content, json_extract(new.metadata, '$.title'),
         new.url, json_extract(new.metadata, '$.path'));
END;
"""

# ── 扩展表：源码挖掘与审核 ──
MINING_REVIEW_SCHEMA_SQL = """
-- api_entities：确定性标识加限定名加库版本归属
CREATE TABLE api_entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_uid TEXT NOT NULL UNIQUE,          -- 语义键哈希，相同即同一实体
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  qualified_name TEXT NOT NULL,
  kind TEXT NOT NULL,                       -- class / function / method
  UNIQUE(version_id, qualified_name, kind)
);

CREATE INDEX idx_api_entities_name ON api_entities(qualified_name);

-- call_instances：调用实例记录，归属库版本，仓库名只做溯源
CREATE TABLE call_instances(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_uid TEXT NOT NULL UNIQUE,            -- 语义键哈希，相同即同一调用
  callee_entity_id INTEGER REFERENCES api_entities(id) ON DELETE SET NULL,
  callee_name TEXT NOT NULL,                -- 别名解析后的限定名
  args JSON NOT NULL DEFAULT '[]',          -- 实参摘要列表
  repo TEXT NOT NULL,
  caller_file TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED','INFERRED','AMBIGUOUS'))
);

CREATE INDEX idx_call_instances_callee ON call_instances(callee_entity_id);

-- seq_patterns：序列挖掘产出
CREATE TABLE seq_patterns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  pattern JSON NOT NULL,                    -- 限定名序列
  support INTEGER NOT NULL,
  is_closed INTEGER NOT NULL DEFAULT 0,
  UNIQUE(version_id, pattern)
);

CREATE INDEX idx_seq_patterns_support ON seq_patterns(version_id, support DESC);

-- review_status：审核工作流
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

# ── 向量索引元数据：向量本体在外部向量服务，库内只记模型名与维度 ──
VECTOR_META_SQL = """
CREATE TABLE schema_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# ── 页面内容指纹：内容不变时短路为不变结局 ──
PAGES_CONTENT_HASH_SQL = """
ALTER TABLE pages ADD COLUMN content_hash TEXT;
"""

# ── 仓库登记与增量缓存，以及调用实例的版本归属补齐 ──
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

# ── 挖掘溯源与查询支撑：提交标识加版本内常用过滤索引 ──
MINING_PROVENANCE_INDEX_SQL = """
ALTER TABLE call_instances ADD COLUMN commit_sha TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_call_instances_version_callee ON call_instances(version_id, callee_name);
CREATE INDEX idx_call_instances_scope ON call_instances(version_id, caller_file, call_order, start_byte);
"""

# ── 分段与关系所需字段：声明签名族与调用拆分族 ──
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

# ── 运维治理表：任务、缓存、留痕、版本互斥，全部幂等建表 ──
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
# ── 分层特征与接口分组：批量构建落库，查询时浏览 ──
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
