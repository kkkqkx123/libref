# P3 混合检索与审核细化方案（修订版）

> 依据：`docs/architecture/tech-stack.md` §5/§6/§7、`docs/architecture/module-layout.md` §1/§2/§3、
> `docs/plan/qdrant-migration.md` §3/§5、`docs/plan/llm-integration.md` §4/§6、
> `docs/plan/feature-tree-cluster-retrieval-loop.md` §5、`docs/ref/docs-mcp-server.md` §4。
> 目标（module-layout §3 P3 行）：最小验证 **RRF 融合检索；审核工作流可执行**。
> 范围：`store/retriever`、`services/{search,review,answer}`、`services/ingest`、`cli`、
> 离线基准。`web/*`、`mcp_server/auth`、`webui/` 移 P3.5（tech-stack §7.3 仍为先 stdio）。

## 0. 现状盘点（以代码为准，原 §0 已过时）

| 已有物 | 状态 | P3 处理 |
|---|---|---|
| `store/vec_store`（集合治理/写入/版本过滤/对账，集合名按库文件派生隔离） | 已落地，对应 qdrant-migration §3/§4 | 直接消费，不改接口 |
| `llm/{embeddings,rerank,chat,gateway}`（批量编码归一校验、重试熔断、缺 key 禁用、重排问答同密钥通道） | 已落地，对应 llm-integration §2/§4 | 直接消费；**不新建 `store/embeddings.py`**（原方案与 module-layout §2 llm 叶定位冲突，作废） |
| `services/ingest._sync_vectors`（先真源后派生、失败计数、跳过 bm25 流、编码文本与 `vec_store.build_embed_text` 共用） | 已落地 | 不动，只补文档说明 |
| `services/search.reembed_library_async`（单版本全量重灌） | 已落地 | 只补 CLI 接线 |
| `store/retriever.HybridRetriever`（双路召回、RRF、重排、频次/分组加成、来源标记） | 已存在但排序语义断裂（见 §2.2） | 本期唯一算法修复点 |
| `services/search.search_docs`（FTS 单路同步）与 `search_hybrid_async`/`answer`（hybrid 异步） | 已并存 | **保持并存**：前者为精确同步查，后者为召回异步查；不强行合并不破坏现有测试，web 检索统一调 hybrid（P3.5 落） |
| `config` RRF/频次/重排/问答参数（`rrf_k/w`、`freq_alpha`、`cluster_*`、`rerank_*`、`chat_*`） | 已就位 | 不改名，只在 §2 锁定语义 |
| `review_status` 表 + ingest 落 AMBIGUOUS pending | 表与生产者就位，无消费方 | 新增 `services/review` 消费 |
| `web/*`、`mcp_server/auth`、`webui/`、`tests/test_retriever.py` | 均不存在，pyproject 无 fastapi/uvicorn | 移 P3.5；P3 只做检索核 + 审核服务 + CLI + 离线基准 |

## 1. 交付物（P3 收敛，P3.5 另行）

P3（本期）：

- `src/libref/store/retriever.py` 改：修复融合分数语义（§2.2）。
- `src/libref/services/review.py` 新：pending 列表（置信优先级）/ approve / mark_conflict / reopen。
- `src/libref/services/__init__.py` 改：导出 review 入口。
- `src/libref/cli.py` 改：新增 `reembed` 与 `review list|approve|conflict|reopen`；`search` 保持 FTS（§2.5）；`serve-mcp --http/--host/--port/--path`（Streamable HTTP，默认 `127.0.0.1:8000/mcp`，只绑本地回环）。
- `tests/test_retriever.py` 新：离线黄金（FakeVec，不依赖 Qdrant/API）+ RRF 行为 + 频次开关 + 降级。

P3.5（不在本期，不进验收）：`web/{app,api}`、`mcp_server/auth`（Bearer + Origin，HTTP 对外暴露的前置条件）、`webui/`（Vite + Vue 3 + Element Plus）、pyproject 加 `fastapi/uvicorn`（注：MCP Streamable HTTP 由 SDK 自带服务承载，不需新增依赖）、段落重组扩展为整节拼装。

## 2. 设计决策

### 2.1 编码传输：复用 `llm/`，不另起模块

编码、重排、问答统一走 `llm/` 传输与 `LlmGateway` 装配（llm-integration §4 已定）。
`retriever` 只收 `embed(vec) / vec_store / rerank` 函数注入，不持有密钥与端点。
无 key 时 `embed` 为空即走 FTS，属预期路径而非报错。
写入与查询的编码文本共用 `vec_store.build_embed_text`（标题路径在前、正文在后），bm25 流不编码不入库向量。

### 2.2 检索流水线（本期唯一算法修复）

顺序：双路召回 → RRF → 同源去重 → 重排（可选）→ 频次/分组有界加成 → 截断。

- 召回：FTS 一路沿用列权重（content 1.0、title 5.0、url 1.0、path 3.0，短精炼字段优先；原方案 `bm25(10,1,5,1)` 为对标项目列序照抄，与本库列序不对应，作废）；向量一路按单版本过滤取近邻。两路各取 `N_c = max(3×limit, 候选下限 30)`，保证融合与加成有材料。
- RRF 只消费排名：`score = w_fts/(k+rank_fts) + w_vec/(k+rank_vec)`，缺席路贡献 0；`k=20、w_fts=1.0、w_vec=0.5` 进配置。候选截断小故不用惯例 60；文档语料全文通常强于向量，故权重向全文倾斜。
- 去重：按 `source_group_id` 同源只留排序最前（双流孪生块合并为一次命中），非段落重组。整节拼装（按 URL 扩展兄弟/父标题块）移 P3.5。
- 重排（默认关）：窗口 `min(max(2×limit,10), rerank_top_n)`，失败标记降级保留融合序。
- 加成（修复点）：此前实现先算加成再按融合序重排回，导致加成为空操作；且 `score` 字段误填名次。本期改为保留 RRF 分数，`final = rrf × (1 + freq_alpha×bonus + cluster_alpha×cluster_bonus)` 并按 final 排序，`score` 即 final。`bonus` 为命中模式支持度按版本内最大值归一到 [0,1]，`cluster_bonus` 为命中同组 ≥2 成员时取 1。加成只作用于融合后 `N_f = max(2×limit,10)` 头部，之外不重排；`α=0` 时为无操作。块到接口归属暂用名称子串启发式，属已知局限（精确归属待源码块与文档块桥接键落定，不在本期）。
- 降级：向量/重排异常由 services 层捕获，标记 `degraded=true` 走 FTS 序；`vec_store` 内不静默降级。未配向量时 FTS 为预期路径，`degraded=false`。未指定 version 时 FTS 横跨版本，向量路仅在解析到单版本时启用并做版本过滤。

### 2.3 审核：收敛到已存在的生产者

`review_status` 仅 `call_instance` 有生产者（ingest 落 AMBIGUOUS pending，删除调用时同步清行）。
本期只做该实体：`list_pending` 按置信 `AMBIGUOUS > INFERRED > EXTRACTED` 再按更新时间；
`approve / mark_conflict` 只接受 pending → 终态，非法转移拒绝；
`reopen` 只接受 conflict → pending。`document / seq_pattern` 保留表语义，无生产者不提供入口，
待桥接键与审核策略落定后再议，不过早收紧检索过滤（冲突条目仍返回，仅附状态）。

### 2.4 出口一致性（本期收敛结论）

`search_docs`（MCP/CLI 同调）保持同步 FTS 精确查；`search_hybrid_async` 与 `answer` 走 hybrid 召回查。
两者用途不同（精确 vs 召回），本期不合并，避免为统一而在同步入口内嵌事件循环或破坏既有基准。
三出口仍只调 `services/`，字段由 `db/schema` 派生，路由内不手拼。

### 2.5 基准即门禁

`tests/test_retriever.py` 离线黄金（FakeVec + 固定 fixtures，不出网）：
MRR/Recall 双轨记录（FTS-only vs 混合），混合增益不足阈值则标红而非放宽；
频次增益单列记录（RRF vs RRF+freq），不混入主门禁；阈值变更需 PR 说明。
重排默认关，不入 P3 验收。

## 3. 验证标准

1. 融合排序按 final 分数而非名次返回；α=0 时与纯 RRF 序一致；缺席路贡献 0。
2. 加成只影响 `N_f` 头部并可提升命中模式的文档；`N_c` 截断生效。
3. 向量/重排失败 → FTS + `degraded=true`；无向量配置 → FTS + `degraded=false`。
4. 重灌：改模型触发重建后 `reembed` 对账一致（SQL 计数 == 向量计数，bm25 流除外）。
5. 审核：AMBIGUOUS 出现在 pending 首部；终态流转与非法拒绝符合 §2.3。
6. `uv run pytest` 全绿；Qdrant/API 不可达时相关测试 skip 而非 fail。
