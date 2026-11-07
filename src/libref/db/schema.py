"""DDL 定义。

蓝本 ref/docs-mcp-server/db/migrations/000-initial-schema.sql。
定制点：直接落地 004/009/014 后的规范化形态（documents 带 library_id/version_id
外键与 url 冗余列），不重演蓝本的多次重建；documents_vec 不在此建表，
由 conn.ensure_vec_table 在 sqlite-vec 可用时延迟创建（P1）。
"""

from __future__ import annotations

# ── 000 初始 schema：核心表 + FTS5 + 触发器 ──
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

-- title/path 存于 documents.metadata JSON（蓝本语义）
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

# ── 001 扩展表：源码挖掘与审核（module-layout §1）──
MINING_REVIEW_SCHEMA_SQL = """
-- api_entities：确定性 ID（graphify ids.py 模式）+ 限定名 + 库版本归属
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

-- call_instances：调用实例记录（行范围 + 实参 + 置信）
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

-- seq_patterns：PrefixSpan 产出
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

-- review_status：审核工作流，挂每条知识条目
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

# ── 002 向量索引元数据（向量本体在 Qdrant，见 docs/plan/qdrant-migration.md）──
# schema_meta 记录 vec_model/vec_dim，供 store 层 ensure_collection 校验失效
VECTOR_META_SQL = """
CREATE TABLE schema_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""
