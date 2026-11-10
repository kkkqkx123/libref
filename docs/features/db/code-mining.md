# 源码挖掘库表：实体、调用、仓库与解析缓存

## 表结构

### api_entities

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| entity_uid | TEXT NOT NULL UNIQUE | 语义哈希，版本加限定名加类型 |
| library_id | INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE | 所属库 |
| version_id | INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE | 所属版本 |
| qualified_name | TEXT NOT NULL | 限定名 |
| kind | TEXT NOT NULL | 类、函数、方法等 |
| parent | TEXT | 父级（版本 5 列） |
| signature | TEXT NOT NULL DEFAULT '' | 签名（版本 5 列） |
| params | JSON NOT NULL DEFAULT '[]' | 参数（版本 5 列） |
| bases | JSON NOT NULL DEFAULT '[]' | 基类（版本 5 列） |
| start_byte | INTEGER NOT NULL DEFAULT 0 | 起始字节（版本 5 列） |
| end_byte | INTEGER NOT NULL DEFAULT 0 | 结束字节（版本 5 列） |
| depth | INTEGER NOT NULL DEFAULT 0 | 深度（版本 5 列） |
| UNIQUE(version_id, qualified_name, kind) | 唯一键 | 同版本同名同类唯一 |

另有 `idx_api_entities_name(qualified_name)`。

### call_instances

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| call_uid | TEXT NOT NULL UNIQUE | 调用哈希，仓库加文件加行范围加被调加实参 |
| callee_entity_id | INTEGER REFERENCES api_entities(id) ON DELETE SET NULL | 被调实体引用 |
| callee_name | TEXT NOT NULL | 解析后限定名 |
| args | JSON NOT NULL DEFAULT '[]' | 实参摘要 |
| repo | TEXT NOT NULL | 仓库名，仅溯源 |
| caller_file | TEXT NOT NULL | 调用文件 |
| line_start | INTEGER NOT NULL | 起始行 |
| line_end | INTEGER NOT NULL | 结束行 |
| confidence | TEXT NOT NULL CHECK (confidence IN ('EXTRACTED','INFERRED','AMBIGUOUS')) | 置信度 |
| library_id | INTEGER REFERENCES libraries(id) ON DELETE CASCADE | 归属（版本 4 列） |
| version_id | INTEGER REFERENCES versions(id) ON DELETE CASCADE | 归属（版本 4 列） |
| caller | TEXT | 调用者（版本 4 列） |
| commit_sha | TEXT NOT NULL DEFAULT '' | 提交标识（版本 6 列） |
| receiver | TEXT | 接收者（版本 5 列） |
| func | TEXT NOT NULL DEFAULT '' | 函数名（版本 5 列） |
| argument_count | INTEGER NOT NULL DEFAULT 0 | 实参数（版本 5 列） |
| call_order | INTEGER NOT NULL DEFAULT 0 | 调用序号（版本 5 列） |
| start_byte | INTEGER NOT NULL DEFAULT 0 | 起始字节（版本 5 列） |
| end_byte | INTEGER NOT NULL DEFAULT 0 | 结束字节（版本 5 列） |
| scope | TEXT | 作用域键，文件加调用者（版本 12 列，已回填） |

索引清单如下：

| 索引 | 列 |
|---|---|
| idx_call_instances_callee | callee_entity_id |
| idx_call_instances_version | (version_id, caller_file) |
| idx_call_instances_repo | (repo, caller_file) |
| idx_call_instances_version_callee | (version_id, callee_name) |
| idx_call_instances_scope | (version_id, caller_file, call_order, start_byte) |
| idx_call_instances_scope_key | (version_id, scope) |

作用域表达式为 `COALESCE(scope, caller_file \|\| char(0) \|\| COALESCE(caller, ''))`，
读路径统一经此表达式，不再逐行拼接。

### repos

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| name | TEXT NOT NULL UNIQUE | 生态加库加版本 |
| url | TEXT | 仓库地址 |
| commit_sha | TEXT | 提交标识 |
| local_path | TEXT NOT NULL | 本地路径 |

### ast_cache

| 列 | 类型与约束 | 说明 |
|---|---|---|
| repo | TEXT NOT NULL，主键前导 | 仓库名 |
| path | TEXT NOT NULL，主键次位 | 相对路径 |
| file_sha1 | TEXT NOT NULL | 文件哈希 |
| fp | TEXT NOT NULL，主键三位 | 语法指纹 |
| cfg_version | INTEGER NOT NULL，主键末位 | 配置版本 |
| records | JSON NOT NULL DEFAULT '[]' | 编码后的声明别名调用记录 |

## 写入

声明与调用均批量幂等写入，冲突时更新除身份外全部字段：

```sql
INSERT INTO api_entities(entity_uid, library_id, version_id,
qualified_name, kind, parent, signature, params, bases,
start_byte, end_byte, depth) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(entity_uid) DO UPDATE SET parent=excluded.parent,
signature=excluded.signature, params=excluded.params, bases=excluded.bases,
start_byte=excluded.start_byte, end_byte=excluded.end_byte,
depth=excluded.depth;

INSERT INTO call_instances(call_uid, callee_entity_id, callee_name, args,
repo, caller_file, line_start, line_end, confidence,
library_id, version_id, caller, receiver, func, argument_count,
call_order, start_byte, end_byte, commit_sha, scope)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(call_uid) DO UPDATE SET callee_entity_id=excluded.callee_entity_id,
callee_name=excluded.callee_name, args=excluded.args,
confidence=excluded.confidence, library_id=excluded.library_id,
version_id=excluded.version_id, caller=excluded.caller,
receiver=excluded.receiver, func=excluded.func,
argument_count=excluded.argument_count, call_order=excluded.call_order,
start_byte=excluded.start_byte, end_byte=excluded.end_byte,
commit_sha=excluded.commit_sha, scope=excluded.scope;
```

实体失效按版本读存活集合，差集分批删除并计数：

```sql
SELECT entity_uid FROM api_entities WHERE version_id=?;   -- 内存求差
DELETE FROM api_entities WHERE version_id=? AND entity_uid IN (...);  -- 每批五百
```

调用失效同样先读后删，附带清理其待审行：

```sql
SELECT id, call_uid FROM call_instances WHERE version_id=?;   -- 内存求差
DELETE FROM review_status WHERE entity_type='call_instance'
AND entity_id IN (...);   -- 每批五百
DELETE FROM call_instances WHERE version_id=? AND call_uid IN (...);  -- 每批五百
```

歧义调用与未解析的推断调用批量建待审行，已存在的不重复：

```sql
SELECT id FROM call_instances WHERE call_uid IN (...);   -- 每批五百
INSERT INTO review_status(entity_type, entity_id, status)
VALUES('call_instance', ?, 'pending')
ON CONFLICT(entity_type, entity_id) DO NOTHING;
```

仓库登记整行覆盖，解析缓存按四元主键覆盖，源码块页缺失文件
由幽灵清扫按地址前缀删除，返回的文档标识进入向量同步增量：

```sql
INSERT INTO repos(name, url, commit_sha, local_path)
VALUES(?, ?, ?, ?)
ON CONFLICT(name) DO UPDATE SET url=excluded.url,
commit_sha=excluded.commit_sha, local_path=excluded.local_path;

INSERT INTO ast_cache(repo, path, file_sha1, fp, cfg_version, records)
VALUES(?, ?, ?, ?, ?, ?)
ON CONFLICT(repo, path, fp, cfg_version)
DO UPDATE SET file_sha1=excluded.file_sha1, records=excluded.records;

SELECT file_sha1, records FROM ast_cache
WHERE repo=? AND path=? AND fp=? AND cfg_version=?;

SELECT path, fp, cfg_version FROM ast_cache WHERE repo=?;   -- 内存求差后分批
DELETE FROM ast_cache WHERE repo=? AND path=? AND fp=? AND cfg_version=?;

SELECT id, url FROM pages WHERE url LIKE ? ESCAPE '\';   -- 前缀为源码地址
DELETE FROM pages WHERE id=?;   -- 逐页，先收文档标识
```

## 读取

频次按被调名分组计数并去重计数作用域，冲突审核的行排除，
歧义行仅在放行开关或已批准时计入：

```sql
SELECT callee_name, count(*),
count(DISTINCT <作用域表达式>)
FROM call_instances LEFT JOIN review_status rs
ON rs.entity_type='call_instance' AND rs.entity_id=call_instances.id
WHERE version_id IN (...) AND (rs.status IS NULL OR rs.status != 'conflict')
[AND (call_instances.confidence != 'AMBIGUOUS' OR rs.status='approved')]
GROUP BY callee_name ORDER BY count(*) DESC, callee_name [LIMIT ?];
```

实参形态按接口加实参原文分组计数倒序：

```sql
SELECT args, count(*) FROM call_instances LEFT JOIN review_status rs
ON rs.entity_type='call_instance' AND rs.entity_id=call_instances.id
WHERE version_id IN (...) AND callee_name = ? [AND ...]
GROUP BY args ORDER BY count(*) DESC LIMIT ?;
```

共现查询先取目标接口的作用域集合，再统计同域其他接口：

```sql
WITH target(s) AS (
 SELECT DISTINCT <作用域表达式>
 FROM call_instances LEFT JOIN review_status rt
 ON rt.entity_type='call_instance' AND rt.entity_id=call_instances.id
 WHERE version_id IN (...) AND callee_name = ? [AND ...])
 SELECT c.callee_name,
 count(DISTINCT <c 作用域表达式>),
 count(*) FROM call_instances c LEFT JOIN review_status rc
 ON rc.entity_type='call_instance' AND rc.entity_id=c.id
 JOIN target t ON <c 作用域表达式> = t.s
 WHERE c.version_id IN (...) AND c.callee_name != ? [AND ...]
 GROUP BY c.callee_name
 ORDER BY 2 DESC, 3 DESC, c.callee_name LIMIT ?;
```

多接口共现先找同时含全部成员的作用域，再在内存汇总域外成员，
调用数回填用分组计数：

```sql
SELECT <h 作用域表达式>, h.callee_name
FROM call_instances h LEFT JOIN review_status rh
ON rh.entity_type='call_instance' AND rh.entity_id=h.id
 JOIN (SELECT <h2 作用域表达式> AS hk
 FROM call_instances h2 LEFT JOIN review_status rh2
 ON rh2.entity_type='call_instance' AND rh2.entity_id=h2.id
 WHERE h2.version_id IN (...) AND h2.callee_name IN (...) [AND ...]
 GROUP BY hk
 HAVING COUNT(DISTINCT h2.callee_name) = ?)
 ON <h 作用域表达式> = hk
 WHERE h.version_id IN (...) [AND ...];

SELECT callee_name, count(*) FROM call_instances
LEFT JOIN review_status rs ON rs.entity_type='call_instance'
AND rs.entity_id=call_instances.id
 WHERE version_id IN (...) [AND ...]
 GROUP BY callee_name;
```

模式证据分两阶段：先找含全部成员的作用域并截断一千，
再只加载这些作用域的调用行，内存按文件分组做子序列匹配：

```sql
SELECT <作用域表达式>
FROM call_instances ci LEFT JOIN review_status rs
ON rs.entity_type='call_instance' AND rs.entity_id=ci.id
 WHERE ci.version_id IN (...)
 AND ci.callee_name IN (...) [AND ...]
 GROUP BY <作用域表达式>
 HAVING COUNT(DISTINCT ci.callee_name) = ?
 ORDER BY 1 LIMIT 1000;

SELECT ci.version_id, l.name, v.version, ci.caller_file, ci.caller,
ci.callee_name, ci.args, ci.line_start, ci.line_end, ci.confidence,
ci.call_order, ci.start_byte, ci.id
 FROM call_instances ci LEFT JOIN review_status rs
ON rs.entity_type='call_instance' AND rs.entity_id=ci.id
 JOIN versions v ON v.id = ci.version_id
 JOIN libraries l ON l.id = v.library_id
 WHERE ci.version_id IN (...) [AND ...]
 AND <作用域表达式> IN (...)
 ORDER BY ci.version_id, ci.caller_file, ci.line_start,
 ci.start_byte, ci.call_order, ci.id;
```
