# store — 知识库与检索

## 职责

真源持久化与检索：文档页面/源码块的存储与读取（SQLite 为真源）、全文索引（FTS5 / PostgreSQL tsvector 双后端）、向量索引（本地 simvec 默认 / Qdrant 可选，可重建派生索引，仅本层可达），以及其上的混合检索流水线（双路召回 → RRF 融合 → 特征过滤 → 重排 → 频率加成 → 窗口扩展 → 证据判定）。

## 关键文件

| 文件 | 职责 |
|---|---|
| `document_store.py` | `DocumentStore`：页面/chunk 写入（复用增量算法）、读取、窗口读取 |
| `fts5_store.py` / `postgres_fts.py` | 两个全文后端的 `FulltextStore` 实现 |
| `retriever/candidates.py` | 双路召回：FTS BM25、向量 KNN、详情补取、版本/租户过滤 |
| `retriever/fusion.py` | RRF 融合、重排、同源去重折叠、跨语言未命中检测 |
| `retriever/assembly.py` | 结果组装、挖掘信号加成、窗口扩展 |
| `retriever/features.py` | block_type/deprecated/aligned_only 后置过滤 |
| `retriever/evidence.py` | 证据充分度判定（可答/弱/不可答三档） |
| `retriever/engine.py` | `HybridRetriever.search_async` 主流程编排 |
| `vec_store/` | 向量后端协议、集合画像治理、simvec/Qdrant 两实现 |
| `snapshots.py` | `SnapshotStore`：按版本落盘抓取/索引原始快照 |

## 功能清单

### 写路径（document_store.py）

`store_page` 在 `write_lock` 内单事务按 outcome 六态分派：`absent` → 删整页；`skipped/failed` → 空增量；`empty` → 清空该页 documents；`unchanged` → 只更新 etag/last-modified；`stored` → **复用增量算法**：

1. upsert 页面后取现有文档，按 chunk 指纹分桶；
2. 新 chunk 命中未被复用过的旧行 → 记"复用"；未被复用的旧行先 DELETE；
3. 复用行先把 `sort_order=-id` 暂存（避免两行互换顺序时撞唯一约束）再 UPDATE 改写；无复用的走 INSERT。

`ensure_changed` 以页面 content_hash 指纹做第二道短路——服务器未返回 304 也能判 unchanged。metadata JSON 记录 title/path/chunk_path/source_group_id/chunk_id/content_hash/keywords/start_char/prev_chunk_id 等全部组装元数据。

### 全文检索（fts5_store.py + candidates.py）

- `_bm25_expr(weights)`：`bm25(documents_fts, w_content, w_title, w_url, w_path)`，四列权重来自配置（默认 1.0/5.0/1.0/3.0）。
- **性能关键**：带版本/租户过滤时用**相关 EXISTS 子查询**而非 `rowid IN (子查询)`——SQLite 会把 IN 子查询物化成无索引列表逐行探测，大语料上退化为秒级；EXISTS 走索引主键探测，毫秒级。
- PostgreSQL 后端实现同一契约：权重归一化的 tsvector 排名表达式 + GIN 索引 + 租户过滤。
- 检索范围解析为 version_ids 时 `ORDER BY v.id DESC`（新版本优先，截断时牺牲最旧）。

### 混合检索流水线（retriever/）

`search_async` 编排：

1. **预算**：`recall_budget = max(candidate_multiplier×limit, rerank_candidate_limit)`，带特征过滤时再乘 multiplier（过滤会砍候选）。
2. **FTS 候选**：BM25 加权召回，预算内取原始分值。
3. **向量候选**：先剔除 (model, dim) 与配置不符的漂移版本（`version_drift` 降级）→ 嵌入查询（失败降级纯 FTS）→ 多版本时截断至 `retrieval_max_versions`（默认 8）并发检索后按 (score, id) 合并。
4. **RRF 融合**：`score(doc) = Σ w_lane/(rrf_k + rank_lane(doc))`，k=20 平滑常数，双通道权重各 1.0。
5. **同源去重折叠**：两条分块通道（BM25 辅助 + embedding 主）索引同一 section；按 alignment key 分组后，凡 (key, part) 已有正文孪生的 BM25 行丢弃；每组最多保留 2 个 chunk（合并 section 合法地拆多块）。
6. **特征过滤**（features.py）：block_type 白名单 / exclude_deprecated / aligned_only（仅 symbol/signature 级对齐的文档），基于 doc_features 与 doc_api_links 表；表缺失时放行。对齐边只用于过滤与结果增强，不产生排序加成。
7. **重排**：只作用于 recall 头部 `window = min(assemble_window, rerank_top_n)`；取正文截断后交 rerank 函数；失败保留融合序并记 `rerank_error` 降级。
8. **组装与挖掘加成**（assembly.py）：

   `final = base × (1 + alpha·bonus + facet_gate·facet_bonus)`

   其中 base = rerank 分优先、否则 RRF 分；bonus 来自模式支持度（实体精确匹配或正文标识符匹配，归一 0..1），facet_bonus 来自查询实体可命中的参数形态。两者都受语料可复用性健康度（`reuse_diversity`）**封顶**——挖掘信号弱时 boost 被压低。文档块特征、实体对齐边与同簇关系不进入排序；排序完成后只对 top-N 附加只读派生注释，不改变 score 与名次。
9. **窗口扩展**：按 page_id 分组取邻域行；对每条结果挑出 `order±radius` 同 lane（chunk_path）的邻块附为 `window` 预览；已与另一条结果相邻的不再附加。
10. **证据判定**（evidence.py）：`pool coverage`（查询单元在候选池的覆盖比例，权重 0.6）+ `head coverage`（最好 3 篇的覆盖，0.4），最佳单篇覆盖 ≥0.5 视为集中答案，分散覆盖打 0.75 折；CJK 查询对无 CJK 语料词法覆盖结构性失效，返回中性 0.5（落 weak 带而非拒答带）。verdict 只报告不过滤——调用方必须能区分"候选弱"与"无候选"。阈值 strong 0.8 / weak 0.5。

### 向量索引与集合治理（vec_store/）

- **集合命名**：以 db 文件绝对路径 SHA1 前 12 位派生（天然隔离），或显式 deployment_id（共享向量服务）。
- **两段画像 `VectorProfile`**：shape 区段（backend、distance、HNSW 图参数、on_disk、shard 等——**仅创建时决定，漂移即删除重建**，维度不匹配同此）与 tuning 区段（hnsw_m/ef_construct/阈值等——远端可**原位 retune**）。None = 用服务默认，不进任何区段。**模型名漂移不重建**：由 `vec_version_meta` 按版本在检索期跳过（version_drift 降级），扩容/重嵌后按版本回灌。
- **Qdrant 后端**：payload 索引 `version_id` 用 `IntegerIndexParams(is_principal=True)`——分区键主索引让过滤先于图遍历执行；tenant 是 KEYWORD 索引；按 `write_batch`（512）分批 upsert，点 id = 文档行 id；搜索期 `hnsw_ef`/exact 可配；default 租户搜索时 limit 放大 3k 后服务端取回 payload 后过滤。
- **simvec 后端**：Rust 绑定动态加载；图参数固定在创建时（shape 区段）；full_scan_threshold 之前走精确扫描、之后发布图。
- **容量门**：写入将越过 `vector_max_points` 上限时硬停（向量是派生物，源真相未动，扩容后批次可重放）。

## 对外接口

`DocumentStore`、`HybridRetriever`、`Fts5FulltextStore`/`PostgresFulltextStore`、`SnapshotStore`、`SimvecVecStore`/`QdrantVecStore`/`VectorProfile`、`filter_by_doc_facets`。

## 依赖与约束

依赖 db（连接）、llm（embed/rerank 经函数注入，不直接 import）、chunking（预算与 doc_facets）。**向量后端仅本层可达**：`store/vec_store` 是唯一持有向量引擎句柄的模块；向量是可重建派生索引，失效后按 SQLite 重灌，不做双写事务。被 services 消费。
