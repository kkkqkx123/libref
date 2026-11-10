# 治理库表：任务、版本锁、缓存与删除日志

## 表结构

### jobs

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| kind | TEXT NOT NULL | 任务类型 |
| params | JSON NOT NULL DEFAULT '{}' | 任务参数 |
| status | TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')) | 状态 |
| error | TEXT | 错误 |
| heartbeat_at | REAL | 心跳时间戳 |
| created_at | TEXT NOT NULL DEFAULT 当前时间 | 创建时间 |
| result | JSON NOT NULL DEFAULT '{}' | 结果摘要 |
| finished_at | TEXT | 完成时间 |

另有 `idx_jobs_recovery(status, heartbeat_at)`。

### version_locks

| 列 | 类型与约束 | 说明 |
|---|---|---|
| version_key | TEXT PRIMARY KEY | 版本键 |
| owner | TEXT NOT NULL | 持有者 |
| heartbeat_at | REAL NOT NULL | 心跳时间戳 |

### robots_cache

| 列 | 类型与约束 | 说明 |
|---|---|---|
| host | TEXT PRIMARY KEY | 主机 |
| body | TEXT NOT NULL | 正文 |
| body_md5 | TEXT NOT NULL | 摘要 |
| fetched_at | REAL NOT NULL | 抓取时间戳 |

### deletion_log

| 列 | 类型与约束 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 行号 |
| library | TEXT NOT NULL | 库 |
| version | TEXT NOT NULL | 版本 |
| ecosystem | TEXT NOT NULL DEFAULT 'custom' | 生态 |
| reason | TEXT NOT NULL | 原因 |
| detail | JSON NOT NULL DEFAULT '{}' | 明细 |
| created_at | TEXT NOT NULL DEFAULT 当前时间 | 创建时间 |

注册表缓存表结构见目录库表文档。

## 查询

任务提交插入行并入队，状态回写持写锁提交。
结果持久化前为摘要形态，长列表截断，
完整输出以各自领域表为准：

```sql
INSERT INTO jobs(kind, params) VALUES(?, ?);

SELECT id, kind, params, status, error, result, finished_at
FROM jobs WHERE id=?;

SELECT id, kind, params, status, error, result, finished_at
FROM jobs ORDER BY id DESC LIMIT ?;

-- 心跳恢复：运行中且心跳过期或缺失的行，按标识升序返回后逐个置失败
SELECT id FROM jobs WHERE status='running' AND (
heartbeat_at IS NULL OR ? - heartbeat_at > ?) ORDER BY id;

-- 状态回写模板（字段名动态拼接）
UPDATE jobs SET <字段>=? WHERE id=?;
```

版本锁声明按版本键插入，唯一冲突才视为争用重试，
其他错误直接上抛。过期持有按心跳比对后以条件更新夺取，
更新零行说明已被他人夺走。续约与释放均持写锁，释放校验持有者。
同步与异步两套守卫共用同一批语句：

```sql
INSERT INTO version_locks(version_key, owner, heartbeat_at) VALUES(?,?,?);

SELECT owner, heartbeat_at FROM version_locks WHERE version_key=?;

UPDATE version_locks SET owner=?, heartbeat_at=?
WHERE version_key=? AND heartbeat_at=?;

UPDATE version_locks SET heartbeat_at=?
WHERE version_key=? AND owner=?;

DELETE FROM version_locks WHERE version_key=? AND owner=?;
```

机器人缓存点查主键，过期与摘要不符视为未命中，
重新抓取后整行覆盖：

```sql
SELECT body, body_md5, fetched_at FROM robots_cache WHERE host=?;

INSERT INTO robots_cache(host, body, body_md5, fetched_at) VALUES(?,?,?,?)
ON CONFLICT(host) DO UPDATE SET body=excluded.body, body_md5=excluded.body_md5,
fetched_at=excluded.fetched_at;
```

删除日志只插不改，过期按创建时间清理：

```sql
INSERT INTO deletion_log(library, version, ecosystem, reason, detail)
VALUES(?,?,?,?,?);

SELECT id, library, version, ecosystem, reason, detail, created_at
FROM deletion_log ORDER BY id DESC LIMIT ?;

DELETE FROM deletion_log WHERE CAST(strftime('%s', created_at) AS REAL) < ?;
```
