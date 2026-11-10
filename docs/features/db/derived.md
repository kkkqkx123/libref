# 派生库表：序列模式、特征树、聚类与语义标签

四表均为按版本重建的派生数据，真源是调用实例，
审核冲突的行不进入构建。标签另依赖模型输出，
缺表时读取返回空，不抛错。

## 表结构

### seq_patterns

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| pattern | JSON NOT NULL | 限定名序列 |
| support | INTEGER NOT NULL | 支持度 |
| is_closed | INTEGER NOT NULL DEFAULT 0 | 闭合标记，写入恒为 1 |
| UNIQUE(version_id, pattern) | 唯一键 | 同版本模式唯一 |

另有 `idx_seq_patterns_support(version_id, support DESC)`。

### feature_nodes

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| path | TEXT NOT NULL | 节点路径 |
| parent | TEXT NOT NULL DEFAULT '' | 父路径 |
| depth | INTEGER NOT NULL | 深度 |
| kind | TEXT NOT NULL DEFAULT 'package' | 类型 |
| calls | INTEGER NOT NULL | 调用数 |
| scopes | INTEGER NOT NULL | 作用域数 |
| UNIQUE(version_id, path) | 唯一键 | 同版本路径唯一 |

另有 `idx_feature_nodes_parent(version_id, parent)`、
`idx_feature_nodes_calls(version_id, calls DESC)`。

### api_clusters

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| cluster_id | INTEGER NOT NULL | 簇号 |
| member | TEXT NOT NULL | 成员限定名 |
| size | INTEGER NOT NULL | 簇大小 |
| representative | TEXT NOT NULL | 代表成员 |
| UNIQUE(version_id, member) | 唯一键 | 同版本成员唯一 |

另有 `idx_api_clusters_version(version_id, cluster_id)`。

### pattern_labels

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| pattern_hash | TEXT NOT NULL | 模式哈希 |
| pattern | JSON NOT NULL | 模式原文 |
| model | TEXT NOT NULL | 模型 |
| prompt_version | TEXT NOT NULL DEFAULT 'v1' | 提示版本 |
| name | TEXT NOT NULL | 名称 |
| summary | TEXT NOT NULL DEFAULT '' | 摘要 |
| category_path | JSON NOT NULL DEFAULT '[]' | 类别路径 |
| workflow | TEXT NOT NULL DEFAULT '' | 工作流 |
| related_apis | JSON NOT NULL DEFAULT '[]' | 关联接口 |
| updated_at | TEXT NOT NULL DEFAULT 当前时间 | 更新时间 |
| UNIQUE(version_id, pattern_hash, model, prompt_version) | 唯一键 | 幂等键 |

另有 `idx_pattern_labels_version(version_id)`。

## 写入

挖掘输入经共享单次扫描加审核过滤后在内存分组，
任一上游为空时统一清空派生表，保证空版本无残留。
三表批量幂等写入后按集合差清理：

```sql
INSERT INTO seq_patterns(library_id, version_id, pattern, support, is_closed)
VALUES(?, ?, ?, ?, 1)
ON CONFLICT(version_id, pattern) DO UPDATE SET support=excluded.support,
is_closed=1;

INSERT INTO feature_nodes(library_id, version_id, path, parent, depth, kind, calls, scopes)
VALUES(?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(version_id, path) DO UPDATE SET parent=excluded.parent,
depth=excluded.depth, kind=excluded.kind,
calls=excluded.calls, scopes=excluded.scopes;

INSERT INTO api_clusters(library_id, version_id, cluster_id, member, size, representative)
VALUES(?, ?, ?, ?, ?, ?)
ON CONFLICT(version_id, member) DO UPDATE SET cluster_id=excluded.cluster_id,
size=excluded.size, representative=excluded.representative;

-- 集合差清理模板（表名与列名按调用点代入，先读存活列再分批删，空集整版清空）
DELETE FROM <表> WHERE version_id=?;
SELECT <列> FROM <表> WHERE version_id=?;   -- 内存求差
DELETE FROM <表> WHERE version_id=? AND <列> IN (...);   -- 每批五百
```

标签幂等键命中直接跳过，形态批量预取，共现保持逐接口查询，
写入按批量提交：

```sql
-- 形态批量预取
SELECT callee_name, args, count(*) FROM call_instances
LEFT JOIN review_status rs ON rs.entity_type='call_instance'
AND rs.entity_id=call_instances.id
 WHERE version_id IN (...) AND callee_name IN (...)
 AND (rs.status IS NULL OR rs.status != 'conflict')
 [AND (call_instances.confidence != 'AMBIGUOUS' OR rs.status='approved')]
 GROUP BY callee_name, args ORDER BY callee_name, 3 DESC;

-- 幂等检查
SELECT count(*) FROM pattern_labels
WHERE version_id=? AND pattern_hash=? AND model=? AND prompt_version=?;

-- 标签写入
INSERT INTO pattern_labels(library_id, version_id, pattern_hash, pattern, model,
prompt_version, name, summary, category_path, workflow, related_apis)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(version_id, pattern_hash, model, prompt_version) DO UPDATE SET
pattern=excluded.pattern, name=excluded.name, summary=excluded.summary,
category_path=excluded.category_path, workflow=excluded.workflow,
related_apis=excluded.related_apis, updated_at=datetime('now');
```

## 读取

模式列表按支持度倒序，无包含过滤时数据库直接截断，
有过滤时游标提前停：

```sql
SELECT pattern, support FROM seq_patterns
 WHERE version_id IN (...)
 ORDER BY support DESC, pattern [LIMIT ?];
```

树浏览按调用数倒序，支持前缀过滤：

```sql
SELECT path, parent, depth, kind, calls, scopes FROM feature_nodes
 WHERE version_id IN (...)
 [AND (path = ? OR path LIKE ? ESCAPE '\')]
 ORDER BY calls DESC, path LIMIT ?;
```

聚类浏览整版加载后内存分组，包含过滤在内存完成：

```sql
SELECT cluster_id, member, size, representative FROM api_clusters
 WHERE version_id IN (...)
 ORDER BY cluster_id, member;
```

标签按更新时间倒序，同一模式哈希只保留最新一行，
截断作用于去重之后：

```sql
SELECT pattern, pattern_hash, model, prompt_version, name,
summary, category_path, workflow, related_apis, updated_at
 FROM pattern_labels
 WHERE version_id IN (...)
 ORDER BY updated_at DESC, pattern_hash, model, prompt_version;
```

问答编排取少量标签作展示增强材料，不参与排序与引用定位。
检索加成按版本加载模式与聚类，加成关闭时跳过加载：

```sql
SELECT version_id, pattern, support FROM seq_patterns
WHERE version_id IN (...);   -- 内存排序后每版本截断五百

SELECT version_id, cluster_id, member FROM api_clusters
WHERE version_id IN (...);   -- 内存按簇号分组
```
