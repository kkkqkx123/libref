# P0 基座细化修改方案

> 依据：`docs/architecture/tech-stack.md` §6/§8、`docs/architecture/module-layout.md` §1/§3。
> 目标（tech-stack §8 第 1 条 + module-layout §3 P0 行）：pyproject 依赖改造 + `db/` 三模块 + `config.py`；
> 最小验证：**建库迁移可重放；FTS 触发器同步正确**。

## 1. 交付物清单

| 文件 | 状态 | 内容 |
|---|---|---|
| `pyproject.toml` | 改 | 新增 `pydantic>=2.8`、`pydantic-settings>=2.4`（tech-stack §6 P0 基座），dev 组补 `ruff`、`mypy` |
| `src/libref/config.py` | 新 | pydantic-settings `LibrefSettings`：db 路径、抓取并发、embedding 模型/维度占位 |
| `src/libref/db/__init__.py` | 新 | 导出 `open_db` / `apply_migrations` |
| `src/libref/db/schema.py` | 新 | 全部 DDL 常量：核心表 + 扩展表（P2/P3 预留）+ FTS5 定义与触发器 |
| `src/libref/db/migrations.py` | 新 | 版本化迁移列表 + `apply_migrations()`（对齐 docs-mcp-server 迁移语义） |
| `src/libref/db/conn.py` | 新 | 连接管理（WAL/外键/busy_timeout）、sqlite-vec 可选加载、`documents_vec` 延迟建表 |
| `tests/conftest.py` | 新 | 临时库连接 fixture |
| `tests/test_migrations.py` | 新 | 迁移可重放、版本号记录、表齐全 |
| `tests/test_fts_sync.py` | 新 | documents 增/改/删 → documents_fts 同步（蓝本 000-initial-schema 触发器语义） |
| `tests/test_config.py` | 新 | 默认值 + 环境变量覆盖 |

## 2. 设计决策

### 2.1 迁移语义（蓝本 ref/docs-mcp-server/db/migrations）

docs-mcp-server 用 `db/migrations/000~019` 纯 SQL 文件 + 迁移表。Python 重写对齐语义但收拢为代码内列表
（P0 只有 2 个迁移，文件化收益低；语义不变：**有序版本号 + 已应用记录 + 事务内执行 + 幂等重放**）：

- `schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)`；
- `apply_migrations(conn)`：查最大已应用版本 → 按序在单事务内执行 `executescript` → 写版本记录；
- 重复调用直接返回（可重放）；任何一条失败整体回滚，不写版本号。

### 2.2 迁移划分

- **000-initial-schema**（蓝本 000-initial-schema.sql + 009-pages 规范化后形态）：
  `libraries / versions / pages / documents / documents_fts` + FTS 三触发器。
  与蓝本差异：蓝本 000 用 `library/version` 文本列，004/009/014 才规范化为 `(library_id, version_id)` 外键——
  我们直接落地规范化终态，避免重演四次重建。
  FTS 为 external-content 表：`content='documents', content_rowid='id'`，`tokenize='porter unicode61'`（蓝本一致）。
- **001-mining-review-schema**（本课题扩展表，module-layout §1）：`api_entities / call_instances / seq_patterns / review_status`，
  含溯源字段（file+line_range+args JSON+confidence）与确定性 ID 唯一约束（graphify ids 模式）。

### 2.3 sqlite-vec / documents_vec 的 P0 处理

sqlite-vec 是 **P1 依赖**（tech-stack §6），P0 不加入 pyproject。因此：
- `documents_vec` **不进版本化迁移**（否则在无扩展环境建库即失败，迁移不可重放）；
- `conn.py` 提供 `load_vec_extension(conn)`（try-import `sqlite_vec`，不可用则返回 False）与
  `ensure_vec_table(conn)`（P1 引入依赖后由 store 层调用建 vec0 虚拟表，rowid=documents.id，(library_id, version_id) 分区）；
- P0 测试仅在 sqlite-vec 可用时才验证该路径，否则 skip。

### 2.4 config.py 字段（P0 只放已确定的）

```python
class LibrefSettings(BaseSettings):
    db_path: Path = Path("data/libref.db")     # LIBREF_DB_PATH
    fetch_concurrency: int = 4                 # LIBREF_FETCH_CONCURRENCY（P1 用）
    request_timeout: float = 30.0              # LIBREF_REQUEST_TIMEOUT（P1 用）
    embedding_model: str = "BAAI/bge-small-zh" # LIBREF_EMBEDDING_MODEL（P3 用）
    embedding_dim: int = 512                   # LIBREF_EMBEDDING_DIM（P3 用）
    model_config = SettingsConfigDict(env_prefix="LIBREF_")
```

模型名/维度记录即 tech-stack §1 "记录模型名+维度，变更时整体失效向量" 的 P0 占位。

### 2.5 依赖规则遵守

P0 的 db 层不 import config（连接由调用方传入路径/连接，services 编排时才用 settings）——
保持 `db` 为最底层，符合 module-layout §2 单向依赖。

## 3. 验证标准（对应 module-layout §3 P0 最小验证）

1. **迁移可重放**：同一连接/新连接上连续调用 `apply_migrations` 两次，第二次为 no-op；
   `schema_migrations` 恰好记录 000、001 两条；核心表 + 扩展表全部存在。
2. **FTS 触发器同步**：insert 后 `documents_fts` 可命中新词；update content 后旧词消失、新词命中；
   delete 后不再命中。用 `fts5vocab` 表核对 token 一致性。
3. `uv run pytest` 全绿。
