# libref 目录结构与模块划分

> 依据：`docs/architecture/tech-stack.md`（选型与分层）与 `docs/ref/overview.md` §五（落地顺序）。
> 原则：**src 布局单包 `libref`**，三层架构（挖掘构建 / 知识库检索 / 双出口）映射为包内模块；三出口只调 `services/` 一层；测试与文档随仓库走，不进发布包。

## 1. 目录树（目标态，随 P0→P4 逐步填充）

```
libref/
├── pyproject.toml              # 唯一依赖声明（uv 管理，见 tech-stack.md §6）
├── uv.lock                     # 锁定全部依赖（含 grammar wheel 版本）
├── AGENTS.md                   # 开发环境约定（uv、测试、代码规范）
├── .python-version             # 3.14
├── docs/
│   ├── req/                    # 课题需求（init-req.md）
│   ├── ref/                    # 对标项目源码级分析（borrow 依据）
│   └── architecture/           # 本目录：tech-stack.md、module-layout.md
├── ref/                        # 外部参考仓库（只读，不参与构建，gitignore）
├── src/libref/
│   ├── __init__.py
│   ├── config.py               # [P0] pydantic-settings：DB 路径、抓取并发、embedding 模型等
│   │
│   ├── db/                     # ── 存储层 ──
│   │   ├── __init__.py
│   │   ├── schema.py           # [P0] 建表 DDL：libraries/versions/pages/documents/
│   │   │                       #      documents_fts + 扩展表
│   │   │                       #      api_entities/call_instances/seq_patterns/review_status/schema_meta
│   │   ├── migrations.py       # [P0] 版本化迁移（对齐 docs-mcp-server 迁移语义）
│   │   └── conn.py             # [P0] 连接管理（PRAGMA）；向量通道在 store/vec_store
│   │
│   ├── crawler/                # ── 分支 A：文档抓取（借鉴 crawl4ai 算法，自研实现）──
│   │   ├── __init__.py
│   │   ├── url_seeder.py       # [P1] robots→sitemap(.gz/index)→llms.txt 链式发现 + HEAD 预筛
│   │   ├── robots.py           # [P1] robots 合规检查（SQLite 缓存 7 天 TTL，照搬 crawl4ai）
│   │   ├── fetcher.py          # [P1] httpx 抓取 + etag/last_modified 增量 + per-host 退避
│   │   ├── extract.py          # [P1] selectolax 正文净化（Pruning）→ markdownify
│   │   └── browser.py          # [P1][可选 extra] Playwright 降级抓取（JS 渲染站点）
│   │
│   ├── chunking/               # ── 分块 ──
│   │   ├── __init__.py
│   │   └── splitter.py         # [P1] 两阶段：标题路径 section.path 结构切分 + 贪心合并
│   │                           #      + 代码围栏平衡不变量
│   │
│   ├── astminer/               # ── 分支 B：源码 AST 抽取（借鉴 graphify）──
│   │   ├── __init__.py
│   │   ├── config.py           # [P2] LanguageConfig 声明式配置（python/java 起步）
│   │   ├── engine.py           # [P2] 通用遍历：类/函数/导入/调用实例（行范围+实参+置信）
│   │   ├── resolution.py       # [P2] 两遍走：声明/导入 facts → 跨文件别名解析
│   │   ├── cache.py            # [P2] 文件 hash + grammar fingerprint 增量缓存
│   │   └── repos.py            # [P2] dulwich 克隆/更新开源仓库、文件遍历
│   │
│   ├── mining/                 # ── 统计与序列挖掘（借鉴 EpiCoder 统计 + PrefixSpan-py）──
│   │   ├── __init__.py
│   │   ├── prefixspan.py       # [P2] 自研 PrefixSpan（closed 支持，加权 key/bound，
│   │   │                       #      callback 流式落库）——蓝本 ref/PrefixSpan-py
│   │   ├── frequency.py        # [P2] 层级频次聚合 + top-N 剪枝（EpiCoder 模式改写）
│   │   ├── cooccur.py          # [P2] (api_i, api_j) 共现矩阵（文件级倒排）
│   │   └── normalize.py        # [P2] 限定名 ↔ 整数 id 映射（对接 api_entities 表）
│   │
│   ├── llm/                    # ── 独立传输层（借鉴 llm-suite 分层思想，仓内顶层包）──
│   │   ├── __init__.py         # 装配入口：三路构造器加公共件与网关
│   │   ├── errors.py           # 错误分类：配置请求远端解码传输超时熔断七变体
│   │   ├── retry.py            # 重试策略：指数退避加等待提示下限加观察回调
│   │   ├── resilience.py       # 传输保护：令牌桶限流加连续失败熔断
│   │   ├── models.py           # 模型目录：已验证模型的数据登记
│   │   ├── embeddings.py       # 编码传输（主后端 SiliconFlow bge-m3，兼容端点）
│   │   ├── rerank.py           # 重排传输（配对重排模型，截断加严格校验）
│   │   ├── chat.py             # 问答传输（非流式加流式，工具调用与用量透出）
│   │   └── gateway.py          # 统一装配：按端点共享弹性状态
│   │
│   ├── store/                  # ── 知识库与检索 ──
│   │   ├── __init__.py
│   │   ├── document_store.py   # [P1/P3] pages/documents 读写、增量结局六态
│   │   ├── vec_store.py        # [P0] 向量双后端（默认本地 simvec，Qdrant 保留）：写入/KNN/集合治理（真源 SQLite 的可重建派生索引）
│   │   ├── evidence.py         # [P3] 证据充分度：候选池与头部词项覆盖 → 可答/弱/不可答
│   │   └── retriever.py        # [P3] FTS5 BM25 + 向量 KNN → RRF → 频次重排 → 段落重组
│   │
│   ├── services/               # ── 业务工具层（三出口唯一调用面）──
│   │   ├── __init__.py         # 立面重导出；出口只调立面或子包显式路径
│   │   ├── factory.py          # 装配根：gateway/向量与各服务装配
│   │   ├── infra/              # 进程级基础设施：无领域依赖
│   │   │   ├── jobs.py         # [P1] 内存队列 + jobs 表 write-through + 心跳恢复
│   │   │   ├── version_lock.py # 版本互斥：DB 行仲裁 + 进程内锁
│   │   │   └── vectors.py      # 网关/向量装配：无向量时降级
│   │   ├── registry/           # 注册表前置：元数据 + 快照缓存 + 模糊发现
│   │   │   ├── libraries.py    # libraries/versions 唯一写点
│   │   │   ├── resolver.py     # 注册表快照缓存与解析
│   │   │   └── discovery.py    # 模糊搜索与确定性排序
│   │   ├── docs/               # 文档分支：抓取编排 + 文档检索问答
│   │   │   ├── ingest.py       # [P1/P2] scrape_docs / refresh_version
│   │   │   ├── search.py       # [P1/P3] search_docs 检索编排
│   │   │   ├── pages.py        # read_page/window/section
│   │   │   ├── answer.py       # 问答编排：混合检索加 grounded 生成
│   │   │   └── answer_materials.py # 引用块与消息组装
│   │   ├── code/               # 源码分支：仓库摄取 + 用法挖掘读写
│   │   │   ├── code_ingest.py  # 克隆/抽取/实体落块
│   │   │   ├── mining_read.py  # 频次/共现/模式/树/聚类读模型
│   │   │   └── mining_refresh.py # 序列模式/树/聚类重算
│   │   └── ops/                # 治理与向量运维：被 docs/code 依赖，不反向依赖
│   │       ├── maintenance.py  # 向量对账/重嵌/reconcile
│   │       ├── retention.py    # 整版本 purge/配额/删除日志
│   │       └── review.py       # [P3] 审核工作流：pending/approved/conflict
│   │
│   ├── mcp_server/             # ── MCP 出口 ──
│   │   ├── __init__.py
│   │   ├── server.py           # [P1] FastMCP 实例与工具注册（只调 services/）
│   │   └── auth.py             # [P3] HTTP 传输令牌鉴权
│   │
│   ├── web/                    # ── Web 出口 ──
│   │   ├── __init__.py
│   │   ├── app.py              # [P3] FastAPI 路由（只调 services/）
│   │   └── api.py              # [P3] REST 模型（pydantic）
│   │
│   └── cli.py                  # [P1] Typer/argparse 入口（只调 services/）
├── webui/                      # [P3] 前端独立工程（npm/Vite + Vue 3 + Element Plus，不进 pyproject）
└── tests/
    ├── conftest.py
    ├── test_prefixspan.py      # [P2] README 黄金样例 + closed 定义校验 + 随机对拍
    ├── test_migrations.py      # [P0] 迁移可重放/失败回滚
    ├── test_fts_sync.py        # [P0] FTS 触发器增/改/删同步
    ├── test_config.py          # [P0] 配置默认值/环境覆盖
    ├── test_simvec_store.py    # 本地 simvec 后端集成测试（构建物缺席自动 skip）
    ├── test_chunking.py        # [P1]
    ├── test_url_seeder.py      # [P1]（本地 fixtures，不联网）
    ├── test_astminer.py        # [P2]（tree-sitter 抽取 vs stdlib ast 抽样 diff）
    ├── test_retriever.py       # [P3] MRR/Recall@k 基准（CI 门禁）
    └── e2e/test_vec_store.py   # Qdrant 后端端到端测试（需 LIBREF_RUN_E2E=1，服务不可达自动 skip；会话共享最小预设集合）
```

## 2. 模块依赖规则（硬约束）

```
crawler ─┐
chunking ├→ db ←─ store ←─ services ←─ {cli, mcp_server, web}
astminer ┘   ↑       ↑
mining ──────┘       llm（传输叶，只依赖 httpx；store 经函数注入消费，services 经构造器装配）
              └→ 向量后端（store/vec_store 唯一触达点：Qdrant 服务或本地 simvec 引擎）
```

1. **单向依赖**：`db` 是唯一被共享的 SQL 底层；`services/` 是唯一编排层；三个出口模块（`cli/mcp_server/web`）**禁止**绕过 services 直接调 store/crawler/mining。
2. **出口模块零业务逻辑**：MCP 工具函数、FastAPI 路由、CLI 命令只做参数解析 + services 调用 + 结果格式化，保证人机行为一致（docs-mcp-server 模式）。
3. **mining 纯内存算法**：不 import db/store，输入为纯 Python 数据（`list[list[int]]`），落库由 services 编排——保持算法可单测、可离线批跑。
4. **astminer 与 crawler 互不依赖**：两条分支仅通过 db 汇合；SourceCode 类网页由 crawler 按 MIME 转投 astminer 的调用点经 services 编排。
5. **跨包边界的 schema 均为 pydantic 模型**：抽取记录、调用实例、MCP 参数、REST 响应共用 `db/schema` 的定义派生，杜绝字段漂移。
6. **向量后端仅 store 层可达**：`store/vec_store.py` 是唯一持有 `qdrant_client` 与 simvec 引擎句柄的模块；SQLite 为唯一真源，向量为可重建派生索引（失效后按 SQLite 重灌，不双写事务，见 docs/plan/qdrant-migration.md 与 docs/plan/simvec-integration.md）。

## 3. 各阶段最小可运行单元（与 tech-stack.md §8 对齐）

| 阶段 | 新增模块 | 最小验证 |
|---|---|---|
| P0 | `db/`、`config.py`、`store/vec_store` | 建库迁移可重放；FTS 触发器同步正确；向量集合治理/版本过滤检索（双后端集成测试） |
| P1 | `crawler/`、`chunking/`、`store/document_store`、`services/{search,ingest,jobs}`、`mcp_server/server`、`cli.py` | sitemap→抓取→分块→FTS→MCP `search_docs` stdio 全链路 |
| P2 | `astminer/`、`mining/` | 仓库→调用实例→频次→closed 序列模式落 `seq_patterns` |
| P3 | `store/{embeddings,retriever}`、`web/`、`mcp_server/auth`、`services/review` | RRF+频次重排检索；Web 审核工作流 |

## 4. 命名与风格约定

- 模块名单数名词（`crawler` 而非 `crawlers`）；文件内符号与 docs/ref 各篇分析中的术语保持一致（LanguageConfig、UrlSeeder、DocumentStore…），便于对照参考实现。
- 每个借鉴自外部项目的模块，在模块 docstring 首行注明蓝本（如 `蓝本 ref/PrefixSpan-py (MIT)`），并标注定制点。
- 测试文件与被测模块一一对应；黄金样例一律标注来源（README/论文）。
