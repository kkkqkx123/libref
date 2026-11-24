# 派生库表：序列模式、特征树、聚类、形态、语义标签与文档特征

十一表均为按挖掘档案重建的派生数据，真源是调用实例；档案绑定版本、语料源集合与参数。同一版本可并存多个档案，
审核冲突的行不进入构建。标签另依赖模型输出，
缺表时读取返回空，不抛错。

代码侧特征语义总纲见 `docs/features/feature-mining.md`，文档侧见 `docs/features/doc-feature-mining.md`；此处只记录表结构与读写口径。

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
| profile_id | INTEGER NOT NULL | 所属挖掘档案 |
| tenant_id | TEXT NOT NULL DEFAULT 'default' | 租户 |
| UNIQUE(profile_id, pattern) | 唯一键 | 同档案模式唯一 |

另有 `idx_seq_patterns_support(version_id, profile_id, support DESC)`。

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
| profile_id | INTEGER NOT NULL | 所属挖掘档案 |
| tenant_id | TEXT NOT NULL DEFAULT 'default' | 租户 |
| UNIQUE(profile_id, path) | 唯一键 | 同档案路径唯一 |

另有 `idx_feature_nodes_parent(version_id, profile_id, parent)`、
`idx_feature_nodes_calls(version_id, profile_id, calls DESC)`。

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
| profile_id | INTEGER NOT NULL | 所属挖掘档案 |
| tenant_id | TEXT NOT NULL DEFAULT 'default' | 租户 |
| UNIQUE(profile_id, member) | 唯一键 | 同档案成员唯一 |

另有 `idx_api_clusters_version(version_id, profile_id, cluster_id)`。

### pattern_labels

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| pattern_hash | TEXT NOT NULL | 模式哈希 |
| pattern | JSON NOT NULL | 模式原文 |
| model | TEXT NOT NULL | 模型 |
| profile_id | INTEGER NOT NULL | 所属挖掘档案 |
| prompt_version | TEXT NOT NULL DEFAULT 'v1' | 提示版本 |
| name | TEXT NOT NULL | 名称 |
| summary | TEXT NOT NULL DEFAULT '' | 摘要 |
| category_path | JSON NOT NULL DEFAULT '[]' | 类别路径 |
| workflow | TEXT NOT NULL DEFAULT '' | 工作流 |
| related_apis | JSON NOT NULL DEFAULT '[]' | 关联接口 |
| updated_at | TEXT NOT NULL DEFAULT 当前时间 | 更新时间 |
| tenant_id | TEXT NOT NULL DEFAULT 'default' | 租户 |
| UNIQUE(profile_id, pattern_hash, model, prompt_version) | 唯一键 | 幂等键 |

另有 `idx_pattern_labels_version(version_id, profile_id)`。

## 写入

挖掘输入经共享单次扫描加审核过滤后在内存分组，
任一上游为空时清空当前档案的派生表，保证空档案无残留。
形态、树、聚类三表批量幂等写入后按集合差清理：

```sql
INSERT INTO seq_patterns(library_id, version_id, profile_id, pattern, support, is_closed)
VALUES(?, ?, ?, ?, ?, 1)
ON CONFLICT(profile_id, pattern) DO UPDATE SET support=excluded.support,
is_closed=1;

INSERT INTO feature_nodes(library_id, version_id, profile_id, path, parent, depth, kind, calls, scopes)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile_id, path) DO UPDATE SET parent=excluded.parent,
depth=excluded.depth, kind=excluded.kind,
calls=excluded.calls, scopes=excluded.scopes;

INSERT INTO api_clusters(library_id, version_id, profile_id, cluster_id, member, size, representative)
VALUES(?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile_id, member) DO UPDATE SET cluster_id=excluded.cluster_id,
size=excluded.size, representative=excluded.representative;

-- 集合差清理模板（表名与列名按调用点代入，先读存活列再分批删，空集整版清空）
DELETE FROM <表> WHERE version_id=? AND profile_id=?;
SELECT <列> FROM <表> WHERE version_id=? AND profile_id=?;   -- 内存求差
DELETE FROM <表> WHERE version_id=? AND profile_id=? AND <列> IN (...);   -- 每批五百
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
WHERE version_id=? AND profile_id=? AND pattern_hash=? AND model=? AND prompt_version=?;

-- 标签写入
INSERT INTO pattern_labels(library_id, version_id, profile_id, pattern_hash, pattern, model,
prompt_version, name, summary, category_path, workflow, related_apis)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile_id, pattern_hash, model, prompt_version) DO UPDATE SET
pattern=excluded.pattern, name=excluded.name, summary=excluded.summary,
category_path=excluded.category_path, workflow=excluded.workflow,
related_apis=excluded.related_apis, updated_at=datetime('now');
```

## 读取

模式列表按支持度倒序，无包含过滤时数据库直接截断，
有过滤时游标提前停：

```sql
SELECT pattern, support FROM seq_patterns
 WHERE profile_id IN (...)
 ORDER BY support DESC, pattern [LIMIT ?];
```

树浏览按调用数倒序，支持前缀过滤：

```sql
SELECT path, parent, depth, kind, calls, scopes FROM feature_nodes
 WHERE profile_id IN (...)
 [AND (path = ? OR path LIKE ? ESCAPE '\')]
 ORDER BY calls DESC, path LIMIT ?;
```

聚类浏览整版加载后内存分组，包含过滤在内存完成：

```sql
SELECT cluster_id, member, size, representative FROM api_clusters
 WHERE profile_id IN (...)
 ORDER BY cluster_id, member;
```

标签按更新时间倒序，同一模式哈希只保留最新一行，
截断作用于去重之后：

```sql
SELECT pattern, pattern_hash, model, prompt_version, name,
summary, category_path, workflow, related_apis, updated_at
 FROM pattern_labels
 WHERE profile_id IN (...)
 ORDER BY updated_at DESC, pattern_hash, model, prompt_version;
```

### feature_facets

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| profile_id | INTEGER NOT NULL | 所属挖掘档案 |
| facet | TEXT NOT NULL | 维度名：实参形态、调用形态、调用序位、组合、守卫 |
| facet_key | TEXT NOT NULL | 维度加取值拼接键，写入与清理用 |
| value | TEXT NOT NULL | 维度取值 |
| calls | INTEGER NOT NULL | 调用数 |
| scopes | INTEGER NOT NULL | 作用域数 |
| tenant_id | TEXT NOT NULL DEFAULT 'default' | 租户 |
| UNIQUE(profile_id, facet_key) | 唯一键 | 同档案维度取值唯一 |

另有 `idx_feature_facets_lookup(version_id, profile_id, facet, calls DESC)`，供浏览与检索加成加载。

### cluster_labels

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| profile_id | INTEGER NOT NULL | 所属挖掘档案 |
| cluster_key | TEXT NOT NULL | 成员排序哈希，幂等键一部分 |
| members | JSON NOT NULL | 成员限定名 |
| model | TEXT NOT NULL | 模型 |
| prompt_version | TEXT NOT NULL | 提示版本 |
| name | TEXT NOT NULL | 名称 |
| summary | TEXT NOT NULL | 摘要 |
| category_path | JSON NOT NULL | 类别路径 |
| workflow | TEXT NOT NULL | 工作流 |
| related_apis | JSON NOT NULL | 关联接口，须落在版本实体集合内 |
| source_note | TEXT NOT NULL | 来源说明必填，写明场景来源与非官方推荐声明 |
| updated_at | TEXT NOT NULL DEFAULT 当前时间 | 更新时间 |
| tenant_id | TEXT NOT NULL DEFAULT 'default' | 租户 |
| UNIQUE(profile_id, cluster_key, model, prompt_version) | 唯一键 | 幂等键 |

另有 `idx_cluster_labels_version(version_id, profile_id)`。

问答编排取少量标签作展示增强材料，不参与排序与引用定位。
检索加成按版本加载模式、聚类与形态，加成关闭时跳过加载：

```sql
SELECT version_id, pattern, support FROM seq_patterns
WHERE profile_id IN (...);   -- 内存排序后每版本截断五百

SELECT version_id, cluster_id, member FROM api_clusters
WHERE profile_id IN (...);   -- 内存按簇号分组

SELECT version_id, value, calls FROM feature_facets
WHERE profile_id IN (...) AND facet='arg_shape';   -- 按版本归一为归属权重
```

形态按调用数倒序，支持维度过滤，归属过滤在内存完成：

```sql
SELECT facet, value, calls, scopes FROM feature_facets
 WHERE profile_id IN (...)
 [AND facet=?]
 ORDER BY calls DESC, value [LIMIT ?];
```

分组标签按更新时间倒序，同一分组键只保留最新一行，
截断作用于去重之后：

```sql
SELECT cluster_key, members, model, prompt_version, name,
summary, category_path, workflow, related_apis, source_note, updated_at
 FROM cluster_labels
 WHERE profile_id IN (...)
 ORDER BY updated_at DESC, cluster_key, model, prompt_version;
```

形态写入与树聚类同形制，批量幂等加集合差清理：

```sql
INSERT INTO feature_facets(library_id, version_id, profile_id, facet,
facet_key, value, calls, scopes) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile_id, facet_key) DO UPDATE SET value=excluded.value,
calls=excluded.calls, scopes=excluded.scopes;
```

## 文档侧派生表

| 表 | 唯一键 | 说明 |
|---|---|---|
| doc_features | (version_id, document_id, facet) | 块结构与内容特征，value 为 `kind` 与 `details` 的 JSON |
| doc_api_links | (version_id, document_id, api_entity_id) | 对齐边，match_kind 为 symbol / signature / name_fuzzy |
| doc_feature_node_links | (profile_id, version_id, document_id, api_entity_id, node_path) | 对齐实体命中的特征树节点及父链 |
| doc_pattern_coverage | (profile_id, document_id, pattern_hash, covered_kind) | 模式覆盖，pattern 保存序列原文 |
| doc_consistency | (profile_id, document_id, facet, model, prompt_version) | 一致性判定与可选模型归纳 |

前三表按版本重建，后三表按档案重建；特征树关联从对齐边与 `feature_nodes` 派生，`links` 视图按块返回命中的节点路径。写入沿用批量幂等加集合差清理，并按同一版本清除后重灌；文档块实体名单由对齐边回写。详细语义见 `docs/features/doc-feature-mining.md`。
