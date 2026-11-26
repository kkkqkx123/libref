# db — schema、迁移与后端契约

## 职责

最底层的共享 SQL 层：SQLite/PostgreSQL 双元数据后端、FTS5/tsvector 双全文后端、版本化迁移链、后端中立契约，以及向量句柄的注入式装配。db 不依赖 `config.py`（不读配置对象，设置由 services 装配时传入）。

## 关键文件

| 文件 | 职责 |
|---|---|
| `schema.py` | 全部 DDL（SQLite 形态），按迁移切段 |
| `migrations.py` | 版本化迁移（v0–v36 共 37 步），单迁移单事务、幂等重放 |
| `contract.py` | `MetadataSession`/`FulltextStore` 协议、错误分类、租户豁免表 |
| `conn.py` | `open_db`（统一 PRAGMA 集）、`write_lock` 可重入写锁 |
| `sqlite_session.py` | 嵌入式元数据会话与错误分类包装 |
| `postgres_session.py` / `postgres_schema.py` | 远端会话（连接池、SQL 方言改写）与合并形态 DDL |
| `assembly.py` / `storage_bundle.py` | 后端选择唯一落点与 `Bundle` 装配 |

## 功能清单

### 连接与写锁（conn.py）

- `open_db` 统一 PRAGMA：`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=NORMAL`、`temp_store=MEMORY`、`cache_size=-64000`。
- `write_lock(handle)`：handle 自带 `write_lock` 属性则直接用，否则惰性挂可重入锁。约定：写者只在同步语句批内持锁，绝不跨 await/sleep 持锁。

### 迁移链（migrations.py + schema.py）

`MIGRATIONS` 是 v0–v36 的有序元组；`apply_migrations` 逐个执行：已应用跳过，否则 `BEGIN → executescript → 记录版本 → commit`，异常整体回滚、版本不记录。可重复调用幂等。要点迁移：

- v0：核心表 `libraries`/`versions`/`registry_cache`/`pages`/`documents` + FTS5 虚表 `documents_fts`（外部内容模式，porter unicode61；title/path 是 `json_extract` 的 STORED 生成列，靠三个触发器维护索引）。
- v1：挖掘基表 `api_entities`（entity_uid = 语义键哈希）/`call_instances`/`seq_patterns`/`review_status`。
- v8：治理表 `jobs`/`robots_cache`/`deletion_log`/`version_locks`。
- v9：`vec_version_meta(version_id, model, dim)`——模型漂移按版本修复，仅维度变化才重建集合。
- v23/v24：多租户（`principals`/`api_keys`/`feedback`/`audit_log`，约 20 张表加 tenant_id）与配额（`quota_policies`/`usage_counters`/`quota_events`）。
- v26：usage 语料与挖掘 profile——`seq_patterns`/`feature_nodes`/`api_clusters` 重建为 profile 域。
- v34/v36：文档代码片段表、`doc_api_links` 用"重建表-插入-改名"方式补列并保持唯一约束。

### 后端契约（contract.py）

- 错误分类：`DbIntegrityError`（唯一/外键，可当冲突处理）、`DbTimeoutError`（瞬态争用，可重试）、`DbOperationalError`。
- `TENANT_EXEMPT_TABLES = {robots_cache, registry_cache, discovery_cache, schema_meta}`：永无租户列的实例本地表。
- `Dialect` Protocol 收敛方言差异四点：`now()`、`json_extract`、`epoch_seconds`、`insert_or_ignore`。
- `MetadataSession` Protocol：服务层唯一接缝，SQL 文本进、普通行出；`table_has_tenant` 动态探测列（先查豁免表）。
- `FulltextStore` Protocol：`search(query, version_ids, weights, budget, tenant_id)` 独立于 SQL 方言。

### SQLite 会话（sqlite_session.py）

`_wrap` 错误分类：IntegrityError → 完整性；OperationalError 且消息含 locked/busy/timeout → 超时；其余操作错误。`table_columns` 用 `PRAGMA table_xinfo`（table_info 会隐藏生成列 title/path）。

### PostgreSQL 会话（postgres_session.py / postgres_schema.py）

- 方言改写：`$.a.b` → `col->'a'->>'b'`；`?` → `%s`（正则按字符串字面量切分，字面量内不动）；`INSERT OR IGNORE` → 补 `ON CONFLICT DO NOTHING`（带 RETURNING 时插在其前）。
- `psycopg_pool` 连接池，每条语句借池化连接、行急切物化（连接归还后 cursor 失效）；`session()` 跨显式事务持有同一连接。
- **不重放 SQLite 迁移链**：`postgres_schema.POSTGRES_SCHEMA_SQL` 是合并后的最终形态 DDL（JSONB、IDENTITY 主键、加权 tsvector 生成列 + GIN 替代 FTS5），只面向全新部署，迁移版本以 `consolidated-<n>` 占位登记。

### 装配（assembly.py / storage_bundle.py）

- `build_metadata_session`：sqlite → `SqliteSession`；postgres → 要求 `postgres_url`；后端选择只发生在一处。
- `build_fulltext_store`：fts5 或 postgres（tsvector）。
- `build_storage_bundle(settings, *, vector)`：向量句柄需 LLM 网关，由 services 层工厂构建后**注入**——db 层不得上探到 services。`Bundle(metadata, fulltext, vector)` 是服务消费的唯一存储面。

## 对外接口

`open_db`、`write_lock`、`MIGRATIONS`、`apply_migrations`/`applied_versions`、`build_metadata_session`、`build_fulltext_store`、`build_storage_bundle`、`SqliteSession`、`PostgresSession`、`MetadataSession`/`FulltextStore`/`StorageBundle` 协议。

## 依赖与约束

零内部依赖的最底层（pydantic-settings 除外不可见——不读 config）；被 store、services 与 crawler（robots 缓存）使用。FTS5 索引写必须走触发器或维护性重建；带过滤的全文检索用相关 EXISTS 而非 `rowid IN`（见 store 文档）。
