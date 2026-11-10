# 向量存储：元数据表与双后端

SQLite 只存模型与维度记录，不存向量。
正文一律回 SQLite 取，点标识与文档行号一一对应。

## 表结构

### schema_meta

| 列 | 类型与约束 | 说明 |
|---|---|---|
| key | TEXT PRIMARY KEY | 键 |
| value | TEXT NOT NULL | 值 |

固定键为最后一次写入的模型名与维度：

```sql
INSERT INTO schema_meta(key, value) VALUES(?, ?)
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
-- 键值对为 (vec_model, 模型) 与 (vec_dim, 维度)
```

### vec_version_meta

| 列 | 类型与约束 | 说明 |
|---|---|---|
| version_id | INTEGER PRIMARY KEY REFERENCES versions(id) ON DELETE CASCADE | 版本 |
| model | TEXT NOT NULL | 嵌入模型 |
| dim | INTEGER NOT NULL | 维度 |
| updated_at | TEXT NOT NULL DEFAULT 当前时间 | 更新时间 |

按版本记录为权威依据，版本行删除时级联清理：

```sql
SELECT model, dim FROM vec_version_meta WHERE version_id=?;

INSERT INTO vec_version_meta(version_id, model, dim) VALUES(?, ?, ?)
ON CONFLICT(version_id) DO UPDATE SET model=excluded.model,
dim=excluded.dim, updated_at=datetime('now');
```

检索前批量读取版本记录，区分从未嵌入与模型漂移：

```sql
SELECT version_id, model, dim FROM vec_version_meta WHERE version_id IN (...);
```

## 双后端

远程与本地两后端共用同一六方法接口，
行为对照如下：

| 方法 | 远程后端 | 本地后端 |
|---|---|---|
| 集合治理 | 建集合加过滤索引，维度变化删建 | 同左，扁平精确扫描形态 |
| 批量写入 | 点号为文档行号，载荷只带库与版本 | 点号为行号字符串，其余相同 |
| 按文档删除 | 按点号删除 | 按字符串点号删除 |
| 按版本删除 | 按版本过滤删除 | 同左 |
| 按版本检索 | 按版本过滤近邻检索，余弦分值降序 | 同左 |
| 计数 | 按版本过滤精确计数 | 同左 |
| 列举 | 按版本滚动列举点号 | 同左 |

集合名按库文件派生，内存库保持基名。
集合治理只在维度变化时删建，模型名漂移不波及其他版本，
由维护层按版本修复。治理临界区另有进程内互斥。

## 同步

抓取与入库先落真源并收集文档增量，再同步向量：
删旧点、编码新文档、批量写入。可嵌入判定为非单路块：

```sql
SELECT id FROM documents WHERE version_id=? AND
(json_extract(metadata, '$.chunk_path') IS NULL
OR json_extract(metadata, '$.chunk_path') != 'bm25')
ORDER BY id;

SELECT count(*) FROM documents WHERE version_id=? AND
(json_extract(metadata, '$.chunk_path') IS NULL
OR json_extract(metadata, '$.chunk_path') != 'bm25');
```

向量失败只计数，不推翻真源写入。

版本对账分三路：模型漂移整版删点后重灌；

```sql
SELECT id, library_id, version_id, content, metadata
FROM documents WHERE version_id=?
AND (json_extract(metadata, '$.chunk_path') IS NULL
OR json_extract(metadata, '$.chunk_path') != 'bm25')
ORDER BY id;
```

集合差按需补缺与删孤儿，缺行按标识批量取回后编码写入：

```sql
SELECT id, library_id, version_id, content, metadata
FROM documents WHERE id IN (...);
```

计数一致且标识集合一致即通过，否则走集合差修复。
重嵌命令整版删除后重写。检索前过滤漂移版本并置降级标记。
