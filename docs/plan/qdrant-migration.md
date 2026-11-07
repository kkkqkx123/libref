# Qdrant 迁移设计方案

> 决策：放弃 sqlite-vec 向量通道，向量存储/检索迁移至 Qdrant（环境已有服务实例，`localhost:6333` 已验证可达）。
> 依据：`docs/plan/sqlite-vec-integration.md`（将被本方案取代的向量部分）、`docs/architecture/tech-stack.md` §1/§5、
> Qdrant 官方文档 multitenancy / points / search（已核实 2026-11）。
> sqlite-vec 保留的判定与放弃原因见 §1；本方案只取代**向量**通道，SQLite 仍是元数据/正文/FTS/审核的唯一真源。

## 1. 迁移动机与 sqlite-vec 通道的处置

sqlite-vec（v0.1.9）KNN 是**纯暴力扫描**，无 ANN 索引；虽然 `version_id` 分区键可把扫描范围收窄到单版本，
但库文档+库代码全量灌入后（预计百万级 chunk），单版本向量数与全库重建成本都会超出其能力；插入格式
（JSON→blob）只是常数优化，不改变容量上限。Qdrant 提供 HNSW ANN、payload 过滤下推、量化与按需
全量扫描（exact=true），是容量问题的根治解。

**处置**：项目未发布、无线上数据，直接替换而非共存——不保留双通道，不写数据搬迁脚本。

## 2. 职责边界与分层（对 module-layout §2 的影响）

```
SQLite（唯一真源，不迁移）：libraries/versions/pages/documents(+FTS)/api_entities/.../schema_meta
Qdrant（可重建的派生索引）：chunk 向量 + 最小 payload，删除后可从 SQLite 全量重灌
```

- 新模块 `src/libref/store/vec_store.py`：`QdrantVecStore`，唯一持有 `qdrant_client` 的地方；
- 依赖规则更新：`store → {db, qdrant_client}`；`services` 仍只调 store 层；三出口不变；
- db 层回归纯 SQL：移除 conn.py 中 sqlite-vec 加载/建表/触发器/knn_search。

## 3. 集合设计（已核实官方 multitenancy 建议）

**单集合 `libref_chunks` + payload 分区过滤**，不为每库/每版本建集合——官方明确：每集合有独立资源开销，
多租户场景默认单集合 + payload 过滤（仅少量大租户才用独立分片/集合）。

```python
client.create_collection(
    collection_name="libref_chunks",
    vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
)
# 版本过滤索引（建集合后、灌数前创建）
client.create_payload_index(
    collection_name="libref_chunks",
    field_name="version_id",
    field_schema=models.IntegerIndexParams(
        type=models.IntegerIndexType.INTEGER, is_principal=True
    ),
)
client.create_payload_index(
    collection_name="libref_chunks", field_name="library_id",
    field_schema=models.PayloadSchemaType.INTEGER,
)
```

- **Point id = documents.id**（无符号整数，天然一一对应，免维护 UUID 映射）；
- **payload 只存 `library_id` / `version_id`**（过滤键），正文与元数据仍在 SQLite，检索命中后按
  doc_id 回 SQLite 取内容——避免双写正文造成漂移（module-layout §2.5 杜绝字段漂移）；
- **distance=COSINE**：bge/MiniLM 系列 embedding 的标准度量。注意与 sqlite-vec 时代语义反转：
  score 越大越相似（旧接口 distance 越小越近），接口层统一命名为 `score` 并注释语义；
- **维度/模型治理沿用 schema_meta**：`vec_model`/`vec_dim` 记在 SQLite。`ensure_collection(model, dim)`
  校验集合维度与 meta，不匹配则删除重建集合（整体失效向量，tech-stack §1 语义）。

## 4. 写入通道（触发器机制的替代）

SQLite 触发器无法跨进程，同步责任上移到 **services 编排层**（`services/ingest.py`，P1 落地）：

```
documents 批量落库（SQLite 事务提交）
  → QdrantVecStore.upsert_batch([(doc_id, version_id, library_id, embedding), ...], wait=True)
```

- 批量 upsert（客户端 `batch` 分批，默认 ~64/批），`wait=True` 保证返回即可查；
- 一致性顺序：先 SQLite 后 Qdrant。Qdrant 是派生索引，**崩溃后的修复策略是重灌而非回滚**：
  提供对账命令 `count(version_id filter)` vs SQL `SELECT count(*) FROM documents WHERE version_id=?`，
  不一致时按 doc_id 差集重灌（幂等，point id 相同即覆盖）；
- 单文档重分块 = 删旧 doc 点 + 插新点；整版本重抓 = `delete(FilterSelector(version_id=...))` 后重灌。

## 5. 检索通道（替代 knn_search）

```python
def search(self, query_vec: list[float], *, k: int, version_id: int) -> list[tuple[int, float]]:
    res = self.client.query_points(
        collection_name=self.collection,
        query=query_vec,
        query_filter=models.Filter(must=[
            models.FieldCondition(key="version_id", match=models.MatchValue(value=version_id)),
        ]),
        limit=k,
        with_payload=False,
    )
    return [(p.id, p.score) for p in res.points]
```

- query 向量生成仍属 P3 `store/embeddings.py`（sentence-transformers），本层只接受已编码向量；
- RRF 融合（P3 retriever）基于**排名**融合，score 语义变化无影响；频次重排若需用相似度分值，
  统一按 COSINE similarity（越大越近）；
- Qdrant 服务不可达时的降级：检索侧回退为 FTS-only（RRF 单路），由 services 层捕获连接异常实现——
  不在 vec_store 内部做静默降级（隐藏失败）。

## 6. 配置与依赖变更

| 项 | 变更 |
|---|---|
| `pyproject.toml` | `uv remove sqlite-vec`；`uv add qdrant-client`（同步/异步双 API，用同步 `QdrantClient`） |
| `config.py` | 新增 `qdrant_url: str = "http://localhost:6333"`、`qdrant_collection: str = "libref_chunks"`、`qdrant_timeout: float = 5.0` |
| `db/migrations.py` | 迁移 002 改为仅 `schema_meta` 表（`documents.embedding` 列、FTS 触发器对 embedding 的感知一并移除——向量不再进 SQLite） |
| `db/conn.py` | 删除 `load_vec_extension / has_vec_extension / ensure_vec_schema / knn_search` |
| `db/schema.py` | 删除 `VECTOR_CHANNEL_SQL` 中 embedding 列部分，002 更名 `vector-meta` |
| `tests/` | `test_vec.py` → `test_vec_store.py`（真连 localhost:6333 集成测试，服务不可达则 skip；用独立测试集合名，teardown 删除） |
| `docs/plan/sqlite-vec-integration.md` | 头部加注"已被 qdrant-migration.md 取代" |

## 7. 迁移步骤与验收

1. `uv remove sqlite-vec && uv add qdrant-client`；
2. db 层裁剪（迁移 002、conn.py、schema.py、对应测试断言）；
3. 新增 `store/vec_store.py`（ensure_collection / upsert_batch / delete_version / search / reconcile_count）；
4. config 新增 qdrant 配置项；
5. `test_vec_store.py` 集成测试：建集合→灌入→版本过滤检索→score 排序→模型/维度变更重建失效→删除同步→对账一致；
6. `uv run pytest` 全绿（Qdrant 不可达时仅 vec_store 测试 skip，其余不受影响）。

**验收标准**：SQLite 迁移仍幂等可重放且不再含向量痕迹；检索返回按 similarity 降序且严格限定
`version_id`；`ensure_collection` 对模型/维度变更触发重建并更新 schema_meta；服务离线不影响其余测试。

## 8. 风险与权衡（记录在案）

| 风险 | 处置 |
|---|---|
| 新增外部服务依赖，破坏"单机零依赖"原初决策 | 用户已确认接受（数据量优先）；Qdrant 是本项目第一个外部服务，后续部署文档须包含它 |
| SQLite 与 Qdrant 双写一致性 | 放弃实时强一致：Qdrant 定位为可重建派生索引 + 对账重灌；不写分布式事务 |
| stdio 本地 MCP 场景要求 Qdrant 在跑 | FTS-only 降级路径（§5）；文档注明最低部署要求 |
| payload 过滤性能 | 灌数前建 `version_id` 整数索引并启用 `is_principal=True`（integer 类型的租户优化开关；keyword/uuid 类型才叫 `is_tenant`，已核实服务端源码 `tenant_optimization()`），符合官方 multitenancy 指南 |
