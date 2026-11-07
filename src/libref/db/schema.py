"""DDL 定义：核心表、全文索引与触发器、扩展表、向量元数据、内容指纹。"""

from __future__ import annotations

# ── 核心表加全文索引与触发器 ──
INITIAL_SCHEMA_SQL = """
CREATE TABLE libraries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  description TEXT
);

CREATE TABLE versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(library_id, version)
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
  entity_uid TEXT NOT NULL UNIQUE,          -- sha1 确定性 ID（含冲突盐）
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  qualified_name TEXT NOT NULL,
  kind TEXT NOT NULL,                       -- class / function / method
  UNIQUE(version_id, qualified_name, kind)
);

CREATE INDEX idx_api_entities_name ON api_entities(qualified_name);

-- call_instances：调用实例记录
CREATE TABLE call_instances(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_uid TEXT NOT NULL UNIQUE,            -- sha1 确定性 ID（含冲突盐）
  callee_entity_id INTEGER REFERENCES api_entities(id) ON DELETE SET NULL,
  callee_name TEXT NOT NULL,                -- 解析别名前的原始限定名
  args JSON NOT NULL DEFAULT '[]',          -- 实参列表
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
