# 文档库表：页面、文档块与全文索引

## 表结构

### pages

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| url | TEXT NOT NULL | 页面地址 |
| etag | TEXT | 条件请求凭证 |
| last_modified | TEXT | 条件请求凭证 |
| fetched_at | TEXT | 抓取时间 |
| content_hash | TEXT | 内容指纹（版本 3 列） |
| UNIQUE(version_id, url) | 唯一键 | 同版本地址唯一 |

### documents

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号，向量点标识即此号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| page_id | INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE | 所属页面 |
| url | TEXT NOT NULL | 块地址 |
| content | TEXT | 正文 |
| metadata | JSON | 块元数据（分路、组标识、块标识、序号链、关键词、实体等） |
| sort_order | INTEGER NOT NULL | 页内排序号 |
| title | TEXT GENERATED ALWAYS AS (json_extract(metadata, '$.title')) STORED | 派生标题 |
| path | TEXT GENERATED ALWAYS AS (json_extract(metadata, '$.path')) STORED | 派生路径 |
| UNIQUE(page_id, sort_order) | 唯一键 | 同页排序号唯一 |

### documents_fts

外部内容虚表，内容表为文档块表，行号为文档行号：

```sql
CREATE VIRTUAL TABLE documents_fts USING fts5(
  content,
  title,
  url,
  path,
  tokenize='porter unicode61',
  content='documents',
  content_rowid='id'
);
```

三触发器分别处理插入、删除、内容相关列更新。
更新触发器只监听内容、元数据、地址、页号四列，
排序号单独变化不扰动索引：

```sql
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
```

索引只经触发器与重建写入，禁止旁路写，
否则后续触发删除会报损坏。

### 索引清单

| 索引 | 列 |
|---|---|
| idx_documents_version | documents(version_id) |
| idx_documents_page | documents(page_id) |
| idx_pages_version | pages(version_id) |
| idx_pages_url | pages(url) |
| idx_documents_url | documents(url) |
| idx_documents_group | documents(url, json_extract(metadata, '$.source_group_id')) |
| idx_documents_chunk_path | documents(version_id, json_extract(metadata, '$.chunk_path')) |

## 写入

单页写入是唯一入口，六种结局（存入、未变、缺席、空、跳过、失败）
一次提交，返回删除、新增、复用三组增量。
复用判定键为分路加标题加路径加正文，同内容不同分路互不复用。
复用行先暂存负排序号避开唯一冲突，再逐行更新；无复用旧行先删。

```sql
-- 页面行幂等写入
INSERT INTO pages(version_id, url, etag, last_modified, fetched_at, content_hash)
VALUES(?,?,?,?,datetime('now'),?)
ON CONFLICT(version_id, url) DO UPDATE SET
etag=excluded.etag, last_modified=excluded.last_modified,
fetched_at=datetime('now'), content_hash=excluded.content_hash;

-- 无复用旧行删除
DELETE FROM documents WHERE id IN (...);

-- 复用行暂存负排序号
UPDATE documents SET sort_order=-id WHERE id IN (...);

-- 复用行更新
UPDATE documents SET url=?, content=?, metadata=?, sort_order=? WHERE id=?;

-- 新行插入
INSERT INTO documents(library_id, version_id, page_id, url, content, metadata, sort_order)
VALUES(?,?,?,?,?,?,?);

-- 未变化只刷新凭证与时间
UPDATE pages SET etag=COALESCE(?, etag),
last_modified=COALESCE(?, last_modified),
fetched_at=datetime('now') WHERE id=?;

-- 整页删除（文档块与全文行经外键级联）
DELETE FROM pages WHERE version_id=? AND url=?;

-- 整页清空
DELETE FROM documents WHERE page_id=?;

-- 内容指纹短路判定在内存完成：页指纹非空且等于新内容哈希即未变
SELECT id, url, etag, last_modified, content_hash
FROM pages WHERE version_id=? AND url=?;
```

## 读取

```sql
-- 按版本列地址，供缺席清扫
SELECT url FROM pages WHERE version_id=? ORDER BY url;

-- 按页取块
SELECT id, url, content, metadata, sort_order FROM documents
WHERE page_id=? ORDER BY sort_order;

-- 按页加排序范围取窗（分路可选）
SELECT id, url, content, metadata, sort_order FROM documents
WHERE page_id=? AND sort_order BETWEEN ? AND ?
[AND json_extract(metadata, '$.chunk_path')=?] ORDER BY sort_order;

-- 按标识批量取块，返回按输入保序
SELECT id, library_id, version_id, url, content, metadata, sort_order
FROM documents WHERE id IN (...) ORDER BY id;

-- 整页重组（左连接保证空页仍返回页行，单路块除外）
SELECT p.id, d.content, d.metadata, d.sort_order, l.name, v.version
FROM pages p
JOIN versions v ON v.id = p.version_id
JOIN libraries l ON l.id = v.library_id
LEFT JOIN documents d ON d.page_id = p.id
AND (json_extract(d.metadata, '$.chunk_path') IS NULL
OR json_extract(d.metadata, '$.chunk_path') != 'bm25')
WHERE p.url = ? [AND l.name = ? AND l.ecosystem = ? AND v.version = ?]
ORDER BY d.sort_order [LIMIT ?];

-- 读窗先定位页，再取窗
SELECT p.id, l.name, v.version FROM pages p
JOIN versions v ON v.id = p.version_id
JOIN libraries l ON l.id = v.library_id
WHERE p.url = ? [AND ...];

-- 读节（单路块除外）
SELECT d.content, d.metadata, d.sort_order FROM documents d
JOIN versions v ON v.id = d.version_id
JOIN libraries l ON l.id = d.library_id
WHERE d.url = ? AND json_extract(d.metadata, '$.source_group_id') = ?
[AND ...] AND (json_extract(d.metadata, '$.chunk_path') IS NULL
OR json_extract(d.metadata, '$.chunk_path') != 'bm25')
ORDER BY d.sort_order [LIMIT ?];
```

## 全文共享读取

两路共用同一函数，返回文档标识与原始分值，分值升序为优。
无过滤时数据库排序截断；有过滤先解析版本集合，
只取集合内候选的行号与分值，内存堆取前若干，无全局排序。
空查询串、空集合、零出数直接返回空：

```sql
-- 无过滤全局路
SELECT documents_fts.rowid, bm25(documents_fts, w0, w1, w2, w3) FROM documents_fts
WHERE documents_fts MATCH ?
ORDER BY bm25(documents_fts, w0, w1, w2, w3) LIMIT ?;

-- 有过滤作用域路（排序截断在内存完成）
SELECT documents_fts.rowid, bm25(documents_fts, w0, w1, w2, w3) FROM documents_fts
WHERE documents_fts MATCH ?
AND rowid IN (SELECT id FROM documents WHERE version_id IN (...));
```

精确检索在此基础上按标识批量取详情行，
内存排序去重后截断，对外分值取反保持越大越好：

```sql
SELECT d.id, d.url, d.content,
json_extract(d.metadata, '$.title') AS title,
json_extract(d.metadata, '$.path') AS path,
l.name AS library, v.version AS version, d.metadata,
d.page_id, d.sort_order
FROM documents d
JOIN versions v ON v.id = d.version_id
JOIN libraries l ON l.id = d.library_id
WHERE d.id IN (...);
```

混合检索另做详情回查、组折叠、模式聚类加成、重排与窗口扩展，
候选详情与窗口均按标识或按页批量取，不逐行查询。

## 全文维护

```sql
-- 重建（幂等全量重灌）
INSERT INTO documents_fts(documents_fts) VALUES('rebuild');

-- 抽查：触发器计数
SELECT count(*) FROM sqlite_master WHERE type='trigger'
AND name IN ('documents_fts_after_insert',
'documents_fts_after_delete', 'documents_fts_after_update');

-- 抽查：首尾抽样
SELECT id, content, title, path FROM documents ORDER BY id LIMIT ?;
SELECT id, content, title, path FROM documents ORDER BY id DESC LIMIT ?;

-- 抽查：逐行定位核对（探针为标题正文中最长英文词或整形查询首项）
SELECT 1 FROM documents_fts WHERE rowid=? AND documents_fts MATCH ? LIMIT 1;
```
