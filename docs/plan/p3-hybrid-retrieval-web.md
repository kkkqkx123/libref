# P3 混合检索与双出口细化修改方案

> 依据：`docs/architecture/tech-stack.md` §5/§6/§7/§8、`docs/architecture/module-layout.md` §1/§3、
> `docs/plan/qdrant-migration.md` §3/§4/§5、`docs/ref/docs-mcp-server.md`。
> 目标（module-layout §3 P3 行）：最小验证 **RRF+频次重排检索；Web 审核工作流**。
> 范围：`store/{embeddings,retriever}`、`services/review`、`web/{app,api}`、`mcp_server/auth`、
> `services/ingest` 向量写入接线、`webui/` 工程骨架。

## 0. 现状盘点

| 已有物 | 状态 | P3 处理 |
|---|---|---|
| `store/vec_store.py`（QdrantVecStore） | P0 已实现全套：ensure_collection/维度失效重建、upsert_batch、delete_version、search、count 对账 | 直接消费，不改接口 |
| `config.py` `embedding_model/embedding_dim/embedding_api_base/embedding_api_key` | 默认值已按 2026-11 决策更新为 `BAAI/bge-m3`/1024/SiliconFlow 端点；key 经 `.env` 注入（gitignore，模板 `.env.example`） | embeddings.py 首用 |
| `review_status` 表 | 迁移 001 已建；P2 落 AMBIGUOUS pending 行 | services/review 消费 |
| `services/search.py` FTS 单路 + snippet | P1 已实现 | 保留为降级路径，升级为 retriever 的一路 |
| `mcp_server/server.py` stdio 工具面 | P1 已实现 | HTTP 传输 + auth 为 P3.5（tech-stack §7.3 决策） |

## 1. 交付物清单

| 文件 | 状态 | 内容 |
|---|---|---|
| `pyproject.toml` | 改 | `fastapi>=0.115`、`uvicorn[standard]>=0.32`（tech-stack §6 P3）；**不新增 embedding 依赖**——决策 2026-11 后走 OpenAI 兼容 API，复用 P1 已有 `httpx`，`sentence-transformers` 退出依赖清单 |
| `src/libref/store/embeddings.py` | 新 | `EmbeddingClient`（主后端 SiliconFlow 免费 `BAAI/bge-m3`）：批编码、超时/重试、模型名/维度与 schema_meta 校验、失效整体重灌；`EmbeddingBackend` Protocol 抽象预留多后端（llama-server 已示范同协议可切，见 tools/hurl/） |
| `src/libref/store/retriever.py` | 新 | 双路召回 → RRF → 频次重排 → 段落重组；Qdrant 不可达降级 FTS-only |
| `src/libref/services/review.py` | 新 | 审核工作流：list pending（按置信优先级）/ approve / mark conflict |
| `src/libref/services/search.py` | 改 | `search_docs` 改走 Retriever；新增 `reembed_library` 编排入口 |
| `src/libref/services/ingest.py` | 改 | stored 结局后批量 upsert 向量（SQLite 提交 → Qdrant，先真源后派生） |
| `src/libref/web/__init__.py` | 新 | — |
| `src/libref/web/api.py` | 新 | REST 模型（pydantic，字段从 db/schema 定义派生，module-layout §2.5） |
| `src/libref/web/app.py` | 新 | FastAPI 路由（只调 services/）：search / pages / libraries / versions / ingest(异步 job) / review；静态托管 webui 构建产物 |
| `src/libref/mcp_server/auth.py` | 新 | Streamable HTTP 传输的 Bearer 令牌鉴权 + Origin 校验（默认仍 stdio） |
| `src/libref/mcp_server/server.py` | 改 | `build_mcp_server(conn, transport=...)`；`serve-http` 启动路径接 auth |
| `src/libref/cli.py` | 改 | 新增 `serve-web` / `reembed <library>` / `review list|approve|conflict`；`serve-mcp --http` 选项 |
| `webui/` | 新 | 独立 npm 工程骨架（**Vite + Vue 3 + Element Plus**，tech-stack §7.1 已定），三页面：检索 / 库管理 / 审核队列 |
| `tests/test_retriever.py` | 新 | **MRR/Recall@k 黄金基准（CI 门禁）**+ RRF 参数行为 + 降级路径 |
| `tests/test_embeddings.py` | 新 | 请求形态断言（httpx MockTransport 离线）+ 模型/维度校验与失效重建；无 key / API 不可达时 skip（模式同 test_vec_store） |
| `tests/test_review.py` | 新 | 状态机 pending→approved/conflict、置信优先级 |
| `tests/test_web_api.py` | 新 | httpx ASGITransport 打路由，不出网 |

## 2. 设计决策

### 2.1 embeddings：模型治理沿用 docs-mcp-server metadata 校验语义

- `EmbeddingClient(settings)`：OpenAI 兼容 `POST {embedding_api_base}/embeddings`
  （Bearer `embedding_api_key`，`.env` 注入）；复用 P1 的 httpx，批编码（≤64/批）、
  429/5xx 指数退避重试，返回向量做 L2 归一化（COSINE 前提）。2026-11 决策：主后端
  SiliconFlow 免费 `BAAI/bge-m3`（1024 维），`tools/hurl/silicon.hurl` 已示范请求形态。
- **文本构造**：`f"{title} / {path}\n{content}"`——标题路径进向量，与 FTS 的 title/path 列对齐。
- 启动/写入前 `ensure_collection(conn, model, dim)`（vec_store 已有）：维度或模型名变更 →
  删除重建集合（整体失效，schema.py 注释语义）→ `reembed_library` 按 SQLite 真源全量重灌；
  增量路径靠 ingest 写入时顺带 upsert，无后台同步。
- 后端抽象（2026-11 修订，替代原"本地优先"）：`EmbeddingBackend` Protocol
  （`encode(texts) -> vectors`）——主后端 API 化后，本地 llama-server 同协议端点仅改
  `embedding_api_base` 即可切换（`tools/hurl/llamacpp.hurl` 已示范）；切回
  sentence-transformers 本地权重也只是"换后端实现"，上层零改动。key 缺失时向量路
  直接禁用（走 FTS 降级，而非报错）。

### 2.2 retriever：融合流水线与参数（本方案的核心定制）

```
查询 ─┬─ FTS5 BM25（P1 单路，sqlite-vec 权重 bm25(10,1,5,1) → documents_fts 列序）→ 候选 fts[N_c]
      └─ Qdrant KNN（embed(查询) → search(k=N_c, version_id)）→ 候选 vec[N_c]
        两路各取 N_c = max(3×limit, 30)（候选截断：段落重组与频次重排要有材料）
→ RRF 融合：score(d) = w_fts/(k + rank_fts(d)) + w_vec/(k + rank_vec(d))，缺席路贡献 0
   k=20、w_fts=1.0、w_vec=0.5 —— 均为 settings 参数（`rrf_k / rrf_w_fts / rrf_w_vec`）；
   不用惯例 k=60：本项目候选截断小，60 会抹平 rank 差（1/61 vs 1/68 相对差 ~10%）；
   等权不取——API 文档语料 BM25 通常显著强于 dense 路。RRF 只消费排名，对两路分值尺度
   （SQLite bm25 负值 vs COSINE similarity）无需归一化（qdrant-migration §5 语义）。
→ 频次重排（创新点）：对融合后 top-N_f（=max(2×limit,10)）内做
   score' = score_rrf × (1 + α·log(1 + support_norm(d)))
   support_norm：chunk 的 URL/文件关联到的 seq_patterns 支持度按 version 归一到 [0,1] 取 max；
   α=0.15（settings `freq_alpha`，≤0.2）——乘法有界 bonus，不做加法大权重，
   否则排序退化为"支持度优先、相关性其次"；N_f 之外不重排。
→ 段落重组：按 URL 分组，命中块向后扩展兄弟块/父标题块至整节，snippet 取扩展后上下文；
   结果项含 url/title/path/score/synthesized snippet + 来源路标记（调试用）。
```

- **rerank 预留（不进 P3 验收）**：SiliconFlow `/v1/rerank`（`bge-reranker-v2-m3`，
  `tools/hurl/rerank.hurl` 已示范请求形态）可作为段落重组前的 top-N_f 精排档——协议与
  embeddings 同构、同一套 key/抽象即可承载；属 P4 基准化调优项，P3 默认关闭。
- **降级**：services 层捕获 Qdrant 连接异常 → FTS-only 单路 + `degraded=true` 标记；
  不在 vec_store 内静默降级（qdrant-migration §5）。单路时 RRF 退化为原排名，语义不断裂。
- `version_id` 必填（多版本库检索必须限定单版本，验收标准同 qdrant-migration §7）；
  library→version 解析在 services（默认取最新版本，显式 version 覆盖）。

### 2.3 基准即门禁（AGENTS.md：检索基准不得静默放宽）

`tests/test_retriever.py` 黄金集（查询→期望 URL 对，来源标注 = 抓取的真实库文档快照 fixtures）：

- 指标：MRR@8 与 Recall@8；**消融双轨**：`FTS-only` vs `RRF 混合`各跑一遍，
  门禁阈值 = FTS-only 基线值 + 0.05 起步（CI 记录两轨，防止向量路悄悄变负贡献）；
- 若混合轨增益 < 阈值 → 该基准**红**而非放宽——由人去决策是否砍向量路（呼应"向量通道
  收益未实证，交给基准裁决"的讨论结论），阈值变更需 PR 说明；
- 频次重排增益同轨单独记录（`RRF` vs `RRF+freq`），不混进主门禁。

### 2.4 review：置信优先级的审核队列

- `list_pending(entity_type=None, limit)`：documents/call_instances/seq_patterns 三类
  pending，**排序 = AMBIGUOUS > INFERRED > EXTRACTED**（graphify 置信标签即审核优先级，
  ref/graphify.md §3），再按 updated_at；
- `approve(entity_type, entity_id)` / `mark_conflict(...)`：状态机
  pending→approved|conflict，conflict→pending 可重开；全走 review_status 表（已建，不新增迁移）；
- 审核结果影响检索：documents 的 approved/conflict 暂不过滤结果（冲突条目仍返回，
  附 review 状态字段）——过滤策略留 P4 基准观察后再定，不过早收紧。

### 2.5 web：三出口一致性约束

- 路由体只有 `参数解析 → services 调用 → pydantic response`（module-layout §2.2），
  与 MCP/CLI 同调用面，天然人机行为一致；
- REST 模型字段从 `db/schema` 派生（§2.5），禁止路由内手拼 dict；
- 静态托管：`webui` 构建产物 `webui/dist` mount 到 `/`，API 前缀 `/api`；开发期 CORS 开
  localhost:5173；
- ingest 触发走 `services/jobs`（P1 已实现队列）：POST `/api/ingest` 返回 job_id，
  GET `/api/jobs/{id}` 查状态——长任务不阻塞 HTTP。

### 2.6 mcp_server/auth（P3.5 预留实现，默认不启用）

- `serve-http` 时包一层 ASGI 校验：`Authorization: Bearer ${LIBREF_MCP_TOKEN}`，
  未配置 token 即拒绝启动 HTTP 传输（不默认开放）；Origin 白名单防 DNS rebinding；
- stdio 路径零变化；决策点 §7.3 维持"先 stdio、HTTP 放 P3.5"——auth 模块与测试本阶段
  就位，部署文档标注最低要求（含 Qdrant 依赖，qdrant-migration §8）。

## 3. 验证标准（对应 module-layout §3 P3 最小验证）

1. **检索链路**：黄金集上 `search_docs` 返回融合结果；构造"两路都命中/仅 FTS 命中/仅向量命中"
   三样例断言 RRF 排序与缺席 0 贡献语义；参数断言：α=0 时频次重排为 no-op、N_c 截断生效。
2. **降级**：Qdrant 停服 → FTS-only + degraded=true，服务恢复 → 自动回到双路（无状态残留）。
3. **向量写入对账**：ingest 后 `vec_store.count(version_id)` == SQL documents 计数；
   改 `embedding_model` → ensure_collection 触发重建 → reembed 重灌 → 对账一致。
4. **审核工作流**：AMBIGUOUS 调用实例（P2 落库）出现在 pending 首位；approve/conflict 状态流转、
   非法转移拒绝；Web 审核接口与 CLI `review` 同结果（三出口一致性抽查）。
5. **门禁**：MRR@8/Recall@8 两轨基线入库（JSON 阈值文件 + 来源标注），CI 命令
   `uv run pytest tests/test_retriever.py`；阈值文件变更需显式 PR 说明。
6. `uv run pytest` 全绿（P0/P1/P2 不回归；Qdrant/embedding API 不可用时相关测试 skip 而非 fail）。
