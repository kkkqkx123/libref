# docs-mcp-server（Grounded Docs）功能分析

> 仓库：https://github.com/arabold/docs-mcp-server（本地 `ref/docs-mcp-server`）
> 许可证 MIT；TypeScript / Node.js。本课题最高优先级对标项目——用 Python 重写其架构，不直接复用 TS 代码。

## 1. 项目定位与总体架构

开源版 Context7：面向 AI 编码助手的**版本感知文档索引服务**。抓取网站 / GitHub / npm / PyPI / 本地文件，按"库 + 版本"分库索引，通过 MCP（stdio / Streamable HTTP / 兼容 SSE）、CLI、Web UI 三种出口提供检索；单机 SQLite，零外部依赖。支持统一模式（单进程）与分布式模式（多 coordinator + worker）。

分层结构：

| 层 | 路径 |
|---|---|
| 入口/CLI | `src/index.ts`、`src/cli/` |
| HTTP 服务 | `src/app/AppServer.ts`（Fastify） |
| MCP | `src/mcp/mcpServer.ts`、`src/mcp/tools.ts`、`src/mcp/startStdioServer.ts` |
| 业务工具层（CLI/MCP 共用） | `src/tools/*.ts`（每个操作一个类） |
| 后台任务管道 | `src/pipeline/`（PipelineManager / Worker / Client） |
| 事件总线 | `src/events/EventBusService.ts` |
| 抓取 | `src/scraper/`（strategies / fetcher / middleware / pipelines 三层） |
| 分块 | `src/splitter/`（SemanticMarkdownSplitter、GreedySplitter 等） |
| 存储检索 | `src/store/`（DocumentStore、DocumentRetrieverService、embeddings/、assembly/） |
| Web UI | `src/web/client/`（React + tRPC + TanStack Query） |
| 数据库迁移 | `db/migrations/000~019*.sql`（schema 事实标准） |

## 2. 文档抓取 pipeline

- **策略层** `src/scraper/strategies/`：WebScraperStrategy（站点爬取，scope 支持 subpages/hostname/domain，include/exclude 正则）、GitHubScraperStrategy、Npm/PyPi、LocalFileStrategy（ZIP/TAR）。默认模式排除 CHANGELOG 等无用文件（`utils/defaultPatterns.ts`）。
- **llms.txt 发现**：`utils/llmsTxtParser.ts` 在起始 URL 目录与站点根寻找 `llms.txt`，并优先尝试 `.md` 变体（如 `/page.html.md`）。
- **抓取层** `src/scraper/fetcher/`：HttpFetcher 遇反爬/JS 页自动降级 BrowserFetcher（Playwright 无头浏览器）；`AutoDetectFetcher` 自动选择；HostBackoff 按主机退避限速。
- **HTML→Markdown 清洗（中间件链）** `src/scraper/middleware/`：Normalization → CheerioParser → Defuddle（正文提取、去导航页脚）→ MetadataExtractor → LinkExtractor → Sanitizer → HtmlToMarkdown。
- **按 MIME 分派的内容管道** `src/scraper/pipelines/`：Html / Markdown / Json / SourceCode（tree-sitter）/ Text / Document（PDF/Office）各一条；可处理类型由各管道 `canProcess()` 自动推导（capability），入队（按扩展名）与抓取时（按 Content-Type）两道闸过滤。
- **增量抓取**：`pages` 表存每 URL 的 `etag` / `last_modified`，refresh 时带 If-None-Match / If-Modified-Since，304 保留旧块；页面结局分 stored / unchanged / absent / empty / skipped / failed 六种语义计数；抓取选项持久化在 versions 表，重刷复用原配置。

## 3. 版本隔离数据模型（schema 见 `db/migrations/`）

```sql
libraries(id PK, name UNIQUE, created_at)
versions(id PK, library_id FK, name /*NULL=无版本*/, created_at,
         status TEXT DEFAULT 'not_indexed',          -- 任务状态（005 迁移）
         progress_pages, progress_max_pages, error_message, started_at, updated_at,
         UNIQUE(library_id, name))
pages(id PK, version_id FK, url, title, etag, last_modified, content_type,
      UNIQUE(version_id, url))                        -- 页面级元数据，增量抓取依据
documents(id PK, page_id FK, content, metadata JSON /*level,path,types*/,
          sort_order, embedding BLOB /*迁移018: float32*/, INDEX(page_id, sort_order))

-- 全文：外部内容表 + 触发器同步
documents_fts USING fts5(content,title,url,path, tokenize='porter unicode61')
-- 向量：sqlite-vec 虚拟表，rowid=documents.id，按 (library_id, version_id) 分区实现版本隔离
documents_vec USING vec0(library_id, version_id, embedding FLOAT[1536])
```

要点：
1. **版本即分区**——所有索引都带 `(library_id, version_id)` 分区键，检索先解析版本再查，天然隔离。
2. **版本行兼作任务记录**——job 状态/进度直接 write-through 到版本表，无需独立 job 表。
3. `metadata` KV 表记录当前 embedding 模型与维度，启动时校验，模型变更则整体失效向量（防维度错配）。
4. FTS/vec 由数据库触发器同步，update 触发器按列收窄（迁移 018），避免无关更新重建全文索引。

## 4. 检索实现（`src/store/DocumentStore.ts::findByContent`）

- **混合检索**：向量 KNN（带 overfetch）与 FTS5 BM25（权重 `bm25(10.0,1.0,5.0,1.0)`，content 最高）两路 CTE 并行，UNION 候选集。
- **RRF 融合**：`rrf = w_vec/(60+vec_rank) + w_fts/(60+fts_rank)`，权重可配；无 embedding 配置时降级纯 FTS。
- **结果重组** `DocumentRetrieverService.ts` + `assembly/`：命中块按 URL 分组 → 聚类 → 向前/向后扩展兄弟块与父块，把碎块拼成完整段落返回。
- 检索时过滤 `metadata.types` 含 `structural` 的纯结构块；embedding 支持 OpenAI / Ollama / Gemini / Bedrock / Azure（LangChain.js 封装，默认 1536 维）。
- 配套确定性检索质量基准（MRR / Recall@k / nDCG@k）+ CI 回归门禁。

## 5. 分块策略（`src/splitter/`）——重点借鉴

两阶段：
1. **SemanticMarkdownSplitter**：按标题层级（H1–H6）、代码围栏、列表、表格做结构感知切分，产出带 `section.level` 和 `section.path`（标题路径）的语义块，保留文档上下文。
2. **GreedySplitter**：贪心合并小碎块，min/preferred/max 三阈值，结构优先于大小（H1/H2 边界不合并）；`TextDocumentSplitter` 维护**代码围栏平衡不变量**（块边界不落在打开的 ``` 内，宁超限不破块）。

## 6. 双出口

### MCP 工具（`src/mcp/mcpServer.ts`，zod schema）

| 工具 | 参数 |
|---|---|
| `search_docs` | library, version?, query, limit=5 |
| `scrape_docs` | url, library, version?, maxPages?, maxDepth?, scope?, include/excludePatterns?, replace? 等 |
| `refresh_version` / `remove_docs` | library, version? |
| `read_page` | library, pathOrUrl, version?, startChunk?, maxChars=30000（分页续读） |
| `list_pages` / `list_libraries` / `find_version` | 版本支持 X-Range 语义匹配 |
| `list_jobs` / `get_job_info` / `cancel_job` | 后台任务管理 |
| `fetch_url` | url |

另有 MCP Resources（`docs://libraries`、versions 模板）；只读模式自动裁掉写入类工具；初始化发送内置使用说明。

### Web UI（`src/web/client/pages/`）

Overview（统计仪表盘）、Libraries（添加库/版本）、LibraryDetail（分块浏览器 ChunkExplorer、抓取配置面板）、Search（在线检索）、Jobs（任务进度/失败卡片）、Settings；WebSocket 订阅事件 + 查询失效实现实时刷新。

### 传输

- stdio：`src/mcp/startStdioServer.ts`；Streamable HTTP：Fastify 路由 `/mcp` + JWT 认证与 Origin/SSRF 校验（`src/auth/`）；兼容 SSE（弃用）。非 TTY 自动选 stdio，否则 HTTP。

## 7. 后台任务队列（`src/pipeline/PipelineManager.ts`）

内存队列 + 并发上限，每次状态变更即时落库（write-through）；进程重启后按"存活心跳"恢复或标失败（QUEUED→RUNNING→COMPLETED/FAILED/CANCELLED）；per-version 锁防并发抓同版本。事件流：进程内 EventBus → tRPC WebSocket 订阅 → Web UI 实时刷新；分布式模式经 `RemoteEventProxy` 桥接并支持重连补发。

## 8. 对本课题（Python 重写）的借鉴清单

1. 版本化 schema（libraries/versions/pages/documents 四表 + 分区键设计）——直接照搬为 SQLite 表结构基础。
2. 版本行兼作任务记录 + 心跳恢复，简化任务状态管理。
3. 两阶段分块（标题路径 section.path + 贪心合并 + 代码围栏平衡）。
4. RRF 混合检索（rank 级融合，比分数归一化稳健）与检索结果段落重组。
5. pages 表分离页面级/块级元数据支撑增量刷新；六种页面结局语义。
6. embedding 模型变更检测与整体失效机制。
7. 双出口共用同一业务工具类（MCP 工具与 Web/CLI 调同一服务层），行为一致。
8. llms.txt 优先、Middleware 链式 HTML 清洗、按 MIME 分派管道。
9. 检索质量基准 + CI 门禁，重写时照搬防退化。
10. Origin/SSRF 校验与子资源黑名单——自托管抓取服务的安全基线。

## 9. 局限性（本课题创新点来源）

仅采集**官方静态文档**：不解析开源代码，无法挖掘真实工程中 API 调用序列、调用先后顺序、参数共现频次；无知识库人工审核与冲突条目处理工作流。

## 论文相关工作表述

Grounded Docs 作为 Context7 的开源替代，实现了第三方库官方文档抓取、多版本索引，并提供 Web 管理界面与 MCP 工具双出口，能够缓解大模型在 API 文档层面的幻觉。但其数据源仅局限于官方文档，无法获取真实工程中的 API 调用顺序、参数使用习惯等统计层面的使用模式。
