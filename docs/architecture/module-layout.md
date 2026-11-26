# libref 目录结构与模块划分（现状态）

> 依据：`docs/architecture/tech-stack.md`（选型与分层）与当前 `src/` 代码实况。
> 原则：**src 布局单包 `libref`**，三层架构（挖掘构建 / 知识库检索 / 三出口）映射为包内模块；三出口只调 `services/` 一层；测试与文档随仓库走，不进发布包。
> 本文描述**当前已落地**的结构；各模块职责细节见 `docs/architecture/modules/` 下的分模块说明。

## 1. 目录树（当前实际结构）

```
libref/
├── pyproject.toml              # 唯一依赖声明（uv 管理）
├── uv.lock                     # 锁定全部依赖（含 grammar wheel 版本）
├── AGENTS.md                   # 开发环境约定（uv、测试、代码规范）
├── .python-version             # 3.14
├── crates/simvec/              # 本地向量引擎（Rust，独立构建，经绑定消费）
├── docs/
│   ├── architecture/           # 本目录：架构总览、链路与分模块说明（modules/）
│   └── …                      # 需求、规划、运维等文档
├── deploy/                     # compose、nginx 示例、配置模板与部署脚本
├── ref/                        # 外部参考仓库（只读，不参与构建，gitignore）
├── src/libref/
│   ├── config.py               # pydantic-settings 配置（预设集 + 覆盖链）
│   ├── config_defaults.py      # 内置默认值
│   ├── config_loader.py        # 配置文件加载与本地注入
│   ├── errors.py               # 统一错误分类
│   ├── limits.py               # 预算与上限常量
│   ├── observe.py              # 日志装配（redacted 快照）
│   │
│   ├── db/                     # ── 存储层：唯一共享 SQL 底层 ──
│   ├── crawler/                # ── 分支 A：文档抓取（借鉴 crawl4ai 算法，自研实现）──
│   ├── chunking/               # ── 结构感知分块 ──
│   ├── astminer/               # ── 分支 B：源码 AST 抽取（借鉴 graphify）──
│   ├── mining/                 # ── 统计与序列挖掘（纯内存算法）──
│   ├── llm/                    # ── 独立传输层（chat/embed/rerank，httpx 传输叶）──
│   ├── store/                  # ── 知识库与检索（真源 + 派生向量索引）──
│   ├── text/                   # 文本归一、预算、BM25、引用片段
│   ├── services/               # ── 业务编排层（三出口唯一调用面）──
│   ├── mcp_server/             # ── MCP 出口 ──
│   ├── web/                    # ── Web 出口（FastAPI）──
│   └── cli/                    # 服务端本机运维入口
├── librefctl/                  # 独立发行的 HTTP-only CLI 客户端（只依赖 httpx）
├── webui/                      # 前端独立工程（npm/Vite + Vue 3 + Element Plus，不进 pyproject）
└── tests/                      # 单测随模块、integration/e2e/fixtures 分层
```

### db/

```
db/
├── schema.py            # 建表 DDL：libraries/versions/pages/documents/documents_fts
│                        #   + api_entities/call_instances/seq_patterns/review_status/schema_meta 等扩展表
├── migrations.py        # 版本化迁移（可重放、失败回滚）
├── conn.py              # 连接管理（PRAGMA）；向量通道在 store/vec_store
├── contract.py          # 后端中立存储契约：MetadataSession / FulltextStore / 错误分级
├── storage_bundle.py    # StorageBundle：元数据 + 全文 + 向量三句柄聚合
├── assembly.py          # 装配根：按配置选择后端，唯一后端选择点
├── sqlite_session.py    # SQLite 本地分支（嵌入式会话）
├── postgres_schema.py   # PostgreSQL 汇总 DDL（远程分支面向新部署，不做旧数据迁移）
└── postgres_session.py  # PostgreSQL 共享分支（psycopg3 连接池，契约全量实现）
```

### crawler/

```
crawler/
├── url_seeder.py        # robots→sitemap(.gz/index)→llms.txt 链式发现 + HEAD 预筛
├── robots.py            # robots 合规检查（SQLite 缓存 TTL，照搬 crawl4ai）
├── fetcher.py           # httpx 抓取 + etag/last_modified 增量 + per-host 退避
├── extract.py           # selectolax 正文净化（Pruning）→ markdownify
├── code_blocks.py       # HTML 代码围栏抽取，供用法语料使用
├── browser.py           # Playwright 降级抓取（可选 extra，JS 渲染站点）
├── http.py              # 共享异步客户端装配（代理、超时、重试）
├── scope.py             # 范围策略：纯决策过滤，探查与抓取共用
├── versioning.py        # 站点版本多数证据判定
├── github.py            # 托管搜索传输
├── websearch.py         # 网页搜索传输
└── registry/            # 各生态注册表负载解析与地址卫生
    ├── pypi.py / npm.py / crates.py / go.py / maven.py / nuget.py
    └── overrides.py + overrides.json   # 地址覆盖规则
```

### chunking/

```
chunking/
├── splitter.py          # 两阶段：标题路径结构切分 + 贪心合并 + 代码围栏平衡；
│                        #   一次切分产出向量与全文两股块流
├── budget.py            # 两路预算计量口径
└── doc_facets.py        # 文档块结构/内容特征与文档-实体对齐边（纯算法）
```

### astminer/

```
astminer/
├── config.py            # LanguageConfig 声明式配置（语言名/语法包/后缀/配置版本）
├── engine.py            # 通用遍历：类/函数/导入/调用实例（行范围+实参+置信）
├── parse.py             # 解析器与查询的指纹缓存装配
├── calls.py             # 调用记录：实参摘要、接收者拆分、调用形态合并
├── declarations.py      # 声明装配：限定名、签名、参数、基类、字节范围
├── resolution/          # 两遍走跨文件别名解析
│   ├── orchestration.py # 解析编排与排序
│   ├── candidates.py    # 候选绑定与消歧（重载按实参数等）
│   ├── imports.py       # 导入别名展开
│   ├── inheritance.py   # 继承链上查与接收者类型回填
│   └── helpers.py
├── cache.py             # 文件 hash + grammar fingerprint 增量缓存
├── context.py           # 注释/文档说明跨度判定
├── c_shadow.py          # C 语言影子声明处理
├── macro_calls.py       # �宏调用识别
├── exports.py           # 源码公共导出别名事实：__all__、export *、pub use
├── stdlib.py            # 标准库与内建表（按语言前置）
├── grouping.py          # 同一调用点多查询命中合并
├── ownership.py         # 调用者归属
├── source_chunk.py      # 源码分块装配（成员原文块 + 预算）
├── code_blocks.py       # Markdown 与 Rust 文档代码围栏抽取
├── language_evidence.py # 块标记、祖先类名、邻近标题等语言证据纯解析
├── snippet_status.py    # 片段解析状态分类：证据、歧义与无目标调用
├── manifests.py / gradle.py  # 清单与构建文件解析（依赖声明）
├── models.py            # 抽取记录 pydantic 模型
├── rules/               # 语言查询规则模块（aliases/entities/text）
└── repos.py             # dulwich 克隆/更新开源仓库、文件遍历
```

### mining/

```
mining/
├── prefixspan.py        # 自研 PrefixSpan（closed 支持，加权 key/bound，流式回调）——蓝本 ref/PrefixSpan-py
├── frequency.py         # 层级频次聚合 + top-N 剪枝（EpiCoder 模式改写）
├── cooccur.py           # (api_i, api_j) 共现矩阵（文件级倒排）
├── normalize.py         # 限定名 ↔ 整数 id 映射（对接 api_entities 表）
├── cluster.py           # 作用域交并比连通聚类的接口分组
├── feature_tree.py      # 结构统计特征沿归属链聚合
├── facets.py            # 形态特征：实参形状、调用形态、样本内序位
├── induction.py         # 模式归纳与标签派生
├── idiom.py             # 惯用法派生
├── scoring.py           # 检索侧加权打分（模式/分组/形态归属权重）
└── doc_alignment.py     # 文档块 ↔ 实体对齐边（纯算法）
```

### llm/

```
llm/
├── __init__.py          # 装配入口：三路构造器加公共件与网关
├── errors.py            # 错误分类：配置/请求/远端/解码/传输/超时/熔断七变体
├── retry.py             # 指数退避重试 + 等待提示下限 + 观察回调
├── resilience.py        # 令牌桶限流 + 连续失败熔断
├── models.py            # 已验证模型的数据登记
├── embeddings.py        # 编码传输（主后端 SiliconFlow bge-m3，兼容端点）
├── rerank.py            # 重排传输（配对重排模型，截断 + 严格校验）
├── chat.py              # 问答传输（非流式 + 流式，工具调用与用量透出）
└── gateway.py           # 统一装配：按端点共享弹性状态
```

### store/

```
store/
├── document_store.py    # pages/documents 读写、增量结局六态
├── fts5_store.py        # 全文本地分支：FTS5 契约面（触发器维护由 schema 承担）
├── postgres_fts.py      # 全文远程分支：关系库原生全文（tsvector 生成列 + ts_rank_cd）
├── snapshots.py         # 向量快照仓库（真源向量状态，供对账与重灌）
├── evidence.py          # 证据充分度：候选池与头部词项覆盖 → 可答/弱/不可答
├── retriever/           # 混合检索引擎
│   ├── engine.py        # 检索主流程编排
│   ├── assembly.py      # 检索组件装配
│   ├── candidates.py    # 双路召回与过滤
│   ├── fusion.py        # RRF 融合与同源去重
│   └── features.py      # 频次/特征加权重排
└── vec_store/           # 向量双后端（真源 SQLite 的可重建派生索引）
    ├── __init__.py      # 契约面：写入/KNN/集合画像治理/索引重放/诊断快照
    ├── simvec_backend.py# 本地 simvec 引擎后端（默认）
    ├── qdrant_backend.py# Qdrant 服务后端（可选）
    ├── profile.py       # 集合画像：结构档漂移即重建，调优档就地重写
    ├── meta.py          # schema_meta 中的向量元数据（模型/维度/画像）
    └── helpers.py
```

### text/

```
text/
├── normalize.py         # 按后缀/MIME 分派归一（HTML→Markdown、空行收敛、源码围栏）
├── bm25.py              # BM25 词项打分（URL/查询）
├── cjk.py               # 中文展开与书写系统判定
├── ident.py             # 标识符切分（驼峰/下划线归一）
├── query.py             # 查询词项处理
�├ embedding.py          # 检索文本组装（向量路口语头部）
└ snippet.py            # 引用片段构造（行边界截断）
```

### services/

```
services/
├── __init__.py          # 立面重导出；出口只调立面或子包显式路径
├── factory.py           # 装配根：网关/向量与各服务装配，按有无密钥降级
├── infra/               # 进程级基础设施：无领域依赖
│   ├── jobs.py          # 内存队列 + jobs 表 write-through + 心跳恢复
│   ├── progress.py      # 断点游标：总量/指纹/水位存取
│   ├── version_lock.py  # 版本互斥：DB 行仲裁 + 进程内锁
│   ├── vectors.py       # 网关/向量装配：无向量时降级
│   ├── runtime.py       # 多副本 drain 与配置快照指纹核对
│   ├── quota.py         # 按主体请求配额
│   ├── metrics.py       # 指标收集
│   ├── tenant.py        # 租户隔离
│   ├── timestamps.py    # 时间戳工具
│   ├── context.py       # 请求上下文
│   └── auth/            # 身份与鉴权：密钥、主体、角色、请求验证
├── registry/            # 注册表前置：元数据 + 快照缓存 + 模糊发现
│   ├── libraries.py     # libraries/versions 唯一写点
│   ├── resolver.py      # 注册表快照缓存与解析
│   └── discovery/       # 跨注册表模糊搜索与确定性排序
│       ├── service.py / ranking.py / models.py / cache.py
│       ├── web_search.py / github_search.py
│       └── registries/  # pypi/npm/crates/go/maven/nuget 解析
├── docs/                # 文档分支：抓取编排 + 文档检索问答
│   ├── ingest.py        # scrape_docs / refresh_version
│   ├── scrape_jobs.py   # 抓取任务载荷与入队
│   ├── scrape_plan.py   # 目标规划：范围过滤 + 阈值 + 预算
│   ├── scrape_sweep.py  # 缺席清扫与配额
│   ├── page_pipeline.py # 单页流水线：抓取/清洗/分块/存储
│   ├── probe.py         # 只读文档探查：发现加过滤后即止，不落库
│   ├── search.py        # search_docs 检索编排
│   ├── pages.py         # read_page/window/section
│   ├── answer.py        # 问答编排：混合检索 + grounded 生成
│   ├── answer_materials.py # 引用块与消息组装
│   ├── feature_refresh.py  # 文档派生阶段：特征与对齐边回写
│   ├── feature_jobs.py  # 派生任务编排
│   ├── doc_insight.py   # 文档链接引用统计
│   └── links.py         # 链接读写
├── code/                # 源码分支：仓库摄取 + 用法挖掘读写
│   ├── code_ingest.py   # 克隆/抽取/实体落块
│   ├── repo_source.py   # 仓库来源解析：显式地址优先，按名解析复用
│   ├── repo_prepare.py / repo_sync.py   # 仓库准备与同步
│   ├── repo_collect.py  # 文件收集与预算
│   ├── repo_chunks.py   # 源码分块
│   ├── repo_ids.py      # 实体与调用身份键
│   ├── repo_persist.py  # 实体与调用落库
│   ├── usage_sources.py # 语料源与挖掘档案
│   ├── usage_ingest.py  # 文档与参考片段调用摄取
│   ├── snippet_records.py # 片段语言证据、解析状态与人工标注回写
│   ├── mining_evidence.py # 模式证据查询
│   ├── probe.py         # 只读仓库探查：目录普查后即止，不解析不落库
│   ├── probe_remote.py  # 远端探查：复用槽/托管接口/无 blob 树/浅克隆逐级降级
│   ├── mining_read/     # 频次/模式/特征树/分组/健康等读模型
│   │   ├── insight.py / api_usage.py / pattern_read.py / feature_read.py
│   │   ├── symbol_read.py / health.py / views.py / common.py
│   └ mining_refresh/    # 序列模式/树/聚类重算
│       ├── runner.py / patterns.py / induction.py / callset.py
│       ├── derived.py / params.py
└ ops/                   # 治理与向量运维：被 docs/code 依赖，不反向依赖
    ├── index_requests/  # 索引申请：申请/审批/等待/读取状态机与作业回写
    │   ├── intake.py / lifecycle.py / runner.py / models.py / queries.py
    ├── maintenance.py   # 向量对账/重嵌/reconcile
    ├── retention.py     # 整版本 purge/配额/删除日志
    ├── review.py        # 审核工作流：pending/approved/conflict
    ├── audit.py         # 审计
    └ feedback.py        # 开发者反馈
```

### 出口（mcp_server / web / cli）

```
mcp_server/
├── server.py            # FastMCP 实例与工具注册（只调 services/）
├── context.py           # 会话上下文
├── contract.py          # MCP 参数与错误信封
└ tools/                # 工具分组注册：retrieval/discovery/mining/ops/index_requests

web/
├── app.py               # FastAPI 装配与 SPA 回退（只调 services/）
├── api.py               # REST 模型（pydantic）
├── routes/              # 路由分组：docs/mining/review/jobs/discovery/
│                        #   index_requests/feedback/admin/diagnostics
├── middleware.py        # 鉴权/配额/请求上下文中间件
├── errors.py            # 错误映射
├── request_context.py   # 请求上下文透传
└ spa.py                 # 前端产物托管与末位回退

cli/
├── parsers.py           # 启动与运维参数定义
├── common.py            # 设置/连接/错误装配
├── ops_commands.py      # 启动、迁移、维护、角色与本机任务
├── local_commands.py    # 本机探查与局部操作
└ key_commands.py        # 密钥管理命令
```

### librefctl/

```
librefctl/src/librefctl/
├── commands.py          # 命令表与远端路由映射
├── transport.py         # 单一 HTTP 传输与错误适配
├── output.py            # 单一 JSON 输出封装
└ app.py                # 参数解析、分发与等待
```

## 2. 模块依赖规则（硬约束）

```
crawler ─┐
chunking ├→ db ←─ store ←─ services ←─ {cli, mcp_server, web}
astminer ┘   ↑       ↑
mining ──────┘       llm（传输叶，只依赖 httpx；store 经函数注入消费，services 经构造器装配）
              └→ 向量后端（store/vec_store 唯一触达点：本地 simvec 引擎或 Qdrant 服务）

librefctl ──HTTP──→ web ──→ services
```

1. **单向依赖**：`db` 是唯一被共享的 SQL 底层；`services/` 是唯一编排层；服务端三个出口模块（`cli/mcp_server/web`）**禁止**绕过 services 直接调 store/crawler/mining。
2. **出口模块零业务逻辑**：MCP 工具函数、FastAPI 路由、本机 CLI 命令只做参数解析 + services 调用 + 结果格式化，保证人机行为一致。`librefctl` 是调用方出口，只经 HTTP 调 Web API，不导入 `libref` 包。
3. **mining 纯内存算法**：不 import db/store，输入为纯 Python 数据（`list[list[int]]`），落库由 services 编排——保持算法可单测、可离线批跑。
4. **astminer 与 crawler 互不依赖**：两条分支仅通过 db 汇合；SourceCode 类网页由 crawler 按 MIME 转投 astminer 的调用点经 services 编排。
5. **跨包边界的 schema 均为 pydantic 模型**：抽取记录、调用实例、MCP 参数、REST 响应共用 `db/schema` 的定义派生，杜绝字段漂移。
6. **向量后端仅 store 层可达**：`store/vec_store` 是唯一持有 `qdrant_client` 与 simvec 引擎句柄的模块；SQLite 为唯一真源，向量为可重建派生索引（失效后按 SQLite 重灌，不双写事务）。集合画像随模型与维度写入 `schema_meta`，并拆为两段：结构档（后端/距离/落盘/分片副本）漂移即重建集合，调优档（图参数/优化器阈值）就地重写、点保留。

## 3. 命名与风格约定

- 模块名单数名词（`crawler` 而非 `crawlers`）；文件内符号与 docs/ref 各篇分析中的术语保持一致（LanguageConfig、UrlSeeder、DocumentStore…），便于对照参考实现。
- 每个借鉴自外部项目的模块，在模块 docstring 首行注明蓝本（如 `Blueprint ref/PrefixSpan-py (MIT)`），并标注定制点。
- 测试文件与被测模块一一对应；黄金样例一律标注来源（README/论文）。
- 两条链路（文档、源码）的现状细节见 `docs/architecture/doc-indexing-chain.md` 与 `docs/architecture/code-indexing-chain.md`；分模块说明见 `docs/architecture/modules/`。
