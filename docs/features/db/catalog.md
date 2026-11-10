# 目录库表：库、版本与注册表缓存

## 表结构

### libraries

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| ecosystem | TEXT NOT NULL DEFAULT 'custom' | 生态 |
| name | TEXT NOT NULL | 库名 |
| description | TEXT | 描述 |
| homepage | TEXT | 主页 |
| docs_url | TEXT | 文档地址 |
| repo_url | TEXT | 仓库地址 |
| resolved_from | TEXT | 解析来源 |
| resolved_at | TEXT | 解析时间 |
| UNIQUE(ecosystem, name) | 唯一键 | 同生态库名唯一 |

### versions

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version | TEXT NOT NULL | 版本号 |
| registry_confirmed | INTEGER NOT NULL DEFAULT 1 | 注册表是否背书 |
| created_at | TEXT NOT NULL DEFAULT 当前时间 | 创建时间 |
| UNIQUE(library_id, version) | 唯一键 | 同库版本号唯一 |

另有 `idx_versions_library(library_id)` 反向索引。

### registry_cache

| 列 | 类型与约束 | 说明 |
|---|---|---|
| ecosystem | TEXT NOT NULL，主键前导 | 生态 |
| name | TEXT NOT NULL，主键后继 | 归一化包名 |
| docs_url | TEXT | 文档地址 |
| candidates | JSON NOT NULL DEFAULT '[]' | 候选地址 |
| versions | JSON NOT NULL DEFAULT '[]' | 版本列表 |
| stable | TEXT | 稳定版 |
| homepage | TEXT | 主页 |
| repo_url | TEXT | 仓库地址 |
| fetched_at | REAL NOT NULL | 抓取时间戳 |

## 查询

过滤转版本标识是各读路径的统一入口，按标识升序返回，
升序保证多版本截断时丢弃集合确定：

```sql
SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id
WHERE l.name = ? AND l.ecosystem = ? AND v.version = ?
ORDER BY v.id
```

过滤条件按调用方传入动态拼接，无过滤时返回全库版本。

库列表附带版本计数：

```sql
SELECT l.id, l.ecosystem, l.name, l.description, l.docs_url, l.resolved_from,
(SELECT count(*) FROM versions v WHERE v.library_id = l.id) AS versions
FROM libraries l ORDER BY l.ecosystem, l.name
```

版本列表附带页面计数（有无生态两种过滤，排序均为创建时间）：

```sql
SELECT v.id, v.version, v.registry_confirmed, v.created_at, l.ecosystem,
(SELECT count(*) FROM pages p WHERE p.version_id = v.id) AS pages
FROM versions v JOIN libraries l ON l.id = v.library_id
WHERE l.name = ? [AND l.ecosystem = ?] ORDER BY v.created_at
```

确保写入持写锁单事务提交，元数据用空值保持语义合并，
确认位每次携带最新解析结论重写：

```sql
INSERT INTO libraries(ecosystem, name) VALUES(?,?)
ON CONFLICT(ecosystem, name) DO NOTHING;
SELECT id FROM libraries WHERE ecosystem=? AND name=?;
UPDATE libraries SET homepage=COALESCE(?, homepage), docs_url=COALESCE(?, docs_url),
repo_url=COALESCE(?, repo_url), resolved_from=COALESCE(?, resolved_from),
resolved_at=datetime('now') WHERE id=?;   -- 仅任一元数据非空时执行
INSERT INTO versions(library_id, version, registry_confirmed) VALUES(?,?,?)
ON CONFLICT(library_id, version) DO NOTHING;
UPDATE versions SET registry_confirmed=? WHERE library_id=? AND version=?;
SELECT id FROM versions WHERE library_id=? AND version=?;
```

注册表缓存点查主键，写入整行覆盖：

```sql
SELECT docs_url, candidates, versions, stable, homepage, repo_url, fetched_at
FROM registry_cache WHERE ecosystem=? AND name=?;

INSERT INTO registry_cache(ecosystem, name, docs_url, candidates, versions,
stable, homepage, repo_url, fetched_at) VALUES(?,?,?,?,?,?,?,?,?)
ON CONFLICT(ecosystem, name) DO UPDATE SET docs_url=excluded.docs_url,
candidates=excluded.candidates, versions=excluded.versions, stable=excluded.stable,
homepage=excluded.homepage, repo_url=excluded.repo_url,
fetched_at=excluded.fetched_at;
```

新鲜度按抓取时间与存续期在服务层判定，不进查询条件。
