# P1 文档链路细化修改方案

> 依据：`docs/architecture/tech-stack.md` §4/§5/§6/§8、`docs/architecture/module-layout.md` §1/§2/§3。
> 目标（tech-stack §8 第 2 条 + module-layout §3 P1 行）：爬虫最小链路
> **sitemap→抓取→净化→分块→FTS→MCP `search_docs` stdio 全链路**。
> 范围：`crawler/`、`chunking/`、`store/document_store`、`services/{search,ingest,jobs}`、
> `mcp_server/server`、`cli.py`（P3 的 `mcp_server/auth`、Web 出口不在本阶段）。

## 0. 依赖与配置改动

| 文件 | 状态 | 内容 |
|---|---|---|
| `pyproject.toml` | 改 | P1 依赖：`httpx[http2]>=0.27`、`selectolax>=0.3`、`markdownify>=0.13`、`bm25s>=0.2`（tech-stack §6 P1）；另加 `mcp>=1.2`（module-layout 将 `mcp_server/server` 划入 P1，早于 §6 的 P3 分组，以 module-layout 为准）；可选 extra `browser = ["playwright>=1.47"]` 声明但不实现（`crawler/browser.py` 延后） |
| `src/libref/config.py` | 改 | 新增 P1 字段：`robots_cache_ttl_days=7`、`crawl_delay_default=1.0`、`fetch_max_retries=3`、`chunk_target_tokens`（分块参数）、`user_agent="libref-crawler/0.1"` |

## 1. 交付物清单

| 文件 | 状态 | 内容 |
|---|---|---|
| `src/libref/crawler/__init__.py` | 新 | 导出 UrlSeeder / RobotsChecker / HttpFetcher / extract_to_markdown |
| `src/libref/crawler/robots.py` | 新 | robots 合规检查 + SQLite 缓存（7 天 TTL，蓝本 crawl4ai `utils.py::RobotsParser` 模式） |
| `src/libref/crawler/url_seeder.py` | 新 | robots→llms.txt→sitemap(.gz/index) 链式发现 + HEAD 存活预筛（本地 fixtures 可测） |
| `src/libref/crawler/fetcher.py` | 新 | httpx AsyncClient 抓取 + etag/last_modified 增量 + per-host 串行限速退避 |
| `src/libref/crawler/extract.py` | 新 | selectolax 正文净化（Pruning 思路）→ markdownify |
| `src/libref/chunking/__init__.py` | 新 | 导出 split_markdown |
| `src/libref/chunking/splitter.py` | 新 | 两阶段：标题路径结构切分 + 贪心合并 + 代码围栏平衡不变量（蓝本 docs-mcp-server SemanticMarkdownSplitter/GreedySplitter 模式） |
| `src/libref/store/document_store.py` | 新 | pages/documents 读写、etag/last_modified 增量、页面结局六态（stored/unchanged/absent/empty/skipped/failed） |
| `src/libref/services/__init__.py` | 新 | — |
| `src/libref/services/search.py` | 新 | `search_docs`（FTS5 bm25）/ `read_page` / `list_libraries` / `list_versions` |
| `src/libref/services/ingest.py` | 新 | `scrape_docs` 编排：UrlSeeder→Robots→Fetcher→extract→split→DocumentStore |
| `src/libref/services/jobs.py` | 新 | 进程内 asyncio 队列 + versions 表 write-through 任务记录 + 心跳恢复 |
| `src/libref/mcp_server/__init__.py` | 新 | — |
| `src/libref/mcp_server/server.py` | 新 | FastMCP 实例，注册 `search_docs`/`read_page`/`list_libraries`/`list_versions`，只调 services/ |
| `src/libref/cli.py` | 改 | Typer 风格 argparse（避免新增依赖）：`ingest`/`search`/`serve-mcp`/`init-db` 子命令，只调 services/ |
| `tests/test_chunking.py` | 新 | 结构切分/贪心合并/围栏平衡黄金样例 |
| `tests/test_url_seeder.py` | 新 | 本地 fixtures（mock httpx transport，不联网）：llms.txt 优先、sitemap.gz/index、robots Disallow 过滤、HEAD 预筛 |
| `tests/test_document_store.py` | 新 | 六态结局、增量 etag 短路、FTS 同步联动 |
| `tests/test_services.py` | 新 | search_docs bm25 命中与过滤、jobs 队列落 versions 表 |

## 2. 设计决策

### 2.1 crawler：自研三件套（蓝本 crawl4ai 思路，不依赖）

- **robots.py**：`RobotsChecker(conn)`。每个 host 首次访问 `GET /robots.txt`，用 stdlib `urllib.robotparser` 解析；
  结果缓存于 SQLite 表 `robots_cache(host TEXT PRIMARY KEY, body TEXT, fetched_at TEXT)`，
  TTL 7 天（`robots_cache_ttl_days`），到期或 MD5 变更后重取（crawl4ai 变更检测语义）。
  提供 `can_fetch(url)` 与 `crawl_delay(host)`；robots 不可达（404/超时）视为全允许。
  缓存表建在 `robots.py` 内 `ensure_robots_cache(conn)`——不入版本化迁移（合规缓存非业务 schema，与 documents_vec 同理）。
- **url_seeder.py**：`UrlSeeder`。发现顺序 llms.txt（`GET /llms.txt`，逐行取以 `- ` 开头的链接）→
  sitemap.xml（支持 `.gz` gzip 解压与 `<sitemapindex>` 递归一层）。URL 过滤：robots `Disallow` 剔除 +
  同 host 限定 + 去重。HEAD 预筛为可选步骤（`prefilter=True` 时剔除 404/5xx 与非 HTML/text MIME）。
  所有网络经传入的 `httpx.AsyncClient`，测试用 `MockTransport` 注入。
- **fetcher.py**：`HttpFetcher`。单 `AsyncClient`（HTTP/2、UA、timeout 来自 settings）；
  pages 表已有 `etag/last_modified` 时带 `If-None-Match/If-Modified-Since`，304 → `unchanged` 结局短路。
  per-host 限速：每 host 记录上次请求时间，间隔 ≥ `crawl_delay`（robots 声明值或默认 1.0s）；
  失败按 `fetch_max_retries` 指数退避（0.5s×2^n），最终失败返回 `failed`。
- **extract.py**：`extract_to_markdown(html, base_url)`。selectolax 剥离 `script/style/nav/footer/aside/noscript`
  及 `role=navigation` 节点，丢弃空文本叶节点（Pruning 思路），取 `<main>`/`<article>`/`<body>` 中文本密度最高者，
  markdownify 转 Markdown；`<pre><code>` 保留围栏。纯函数、零网络。

### 2.2 chunking：两阶段分块

`split_markdown(md, source_url, target_chars≈1600)`：

1. **结构切分**：按 ATX 标题行递归切分，每块记录标题路径 `path`（如 `"Guide / Install / Pip"`，`/` 连接）；
   代码围栏内 `#` 开头行不视为标题（围栏状态机跟踪）。
2. **贪心合并**：过小的相邻同级块向后合并直至接近 `target_chars`；超长单块（如一个大代码块）按
   空行硬切并保证**代码围栏平衡**——切点必须落在围栏外，否则后移切点，输出块内 ``` 必须成对。

产出 `Chunk(url, path, title, content, sort_order)` 列表，metadata JSON 落 `{title, path}`（schema.py FTS 触发器消费）。

### 2.3 document_store：六态结局

`DocumentStore(conn)`，蓝本 docs-mcp-server 增量语义：

```
stored     新页面或内容变化：upsert pages(etag/last_modified/fetched_at) + 删旧 documents + 插入新 chunks
unchanged  304 / 内容 hash 未变：仅更新 fetched_at，documents 不动
absent     404/410：删 pages 及级联 documents
empty      抓取成功但正文净化后为空：记录结局，不建 documents
skipped    robots 禁止 / HEAD 预筛剔除
failed     重试耗尽 / 网络错误
```

`store_page(version_id, url, etag, last_modified, chunks, outcome)` 为唯一写入口；
内容 hash 存于 pages 扩展列 `content_hash`（迁移 3：`ALTER TABLE pages ADD COLUMN content_hash TEXT`，
列入 MIGRATIONS 保证可重放）。FTS 由既有触发器自动同步，本层不做 FTS 双写。

### 2.4 services 编排（唯一业务层）

- **search.py**：`SearchService(conn)`。
  `search_docs(query, library=None, version=None, limit=8)`：FTS5 `MATCH` + `bm25(documents_fts)` 排序，
  返回 `{url, title, path, snippet(documents_fts, 1, '>>>', '<<<'), score}`；Qdrant KNN/RRF 融合留 P3（retriever.py）。
  `read_page(url, library, version)` 按 URL 取该页全部 documents 按 sort_order 重组全文；
  `list_libraries` / `list_versions` 纯 SQL。
- **ingest.py**：`IngestService(settings, conn)`。`scrape_docs(library, version, start_url, max_pages=None)`：
  ensure library/versions 行 → UrlSeeder（经 RobotsChecker 过滤）→ 有界信号量并发（`fetch_concurrency`）
  Fetcher 抓取 → 304 短路 → extract → split → DocumentStore 写入 → 返回六态计数 dict。
  同步封装（asyncio.run 内聚合），出口层无需感知 async。
- **jobs.py**：`JobManager`。进程内 `asyncio.Queue` + worker 协程消费；
  每个任务 write-through 一行 versions 扩展表 `jobs(id, kind, params JSON, status, heartbeat_at, error)`；
  worker 循环内更新 `heartbeat_at`；启动时 `recover()` 将 `running` 且心跳超时的任务标 `failed(stale)`（心跳恢复语义）。
  jobs 表同样由 `ensure_jobs_table` 建表（运维表不入业务迁移），置于 jobs.py。

### 2.5 出口层零业务逻辑

- **mcp_server/server.py**：`build_mcp_server(conn)` 返回 FastMCP 实例并注册四个工具，工具体只有
  settings 解析 + `SearchService` 调用 + 结果转 dict；stdio 传输由 CLI `serve-mcp` 启动 `mcp.run("stdio")`。
- **cli.py**：stdlib argparse（不引入 typer，控制 P1 依赖面）。子命令 `init-db` / `ingest` /
  `search <query> [--library --version --limit]` / `read-page <url>` / `serve-mcp`。
  每个子命令 = 参数解析 + services 调用 + 打印。

### 2.6 依赖规则核对（module-layout §2）

crawler/chunking 只依赖 db（document_store）与自身；services 编排一切；mcp_server/cli 只 import services；
mining/P2 不受影响；Qdrant 触达仍在 vec_store（本阶段不涉及）。

## 3. 验证标准（对应 module-layout §3 P1 最小验证）

1. `test_chunking`：标题路径正确、贪心合并后块长 ≤ 阈值×容忍、所有输出块围栏成对。
2. `test_url_seeder`（MockTransport 本地 fixtures，不联网）：llms.txt 优先于 sitemap；sitemap.gz 与 index 均可解析；
   robots Disallow 的 URL 被剔除；重复 URL 去重。
3. `test_document_store`：六态结局各自落库正确；etag 不变 → unchanged 且 documents 不重建；
   delete page 级联清 FTS（复用 P0 触发器语义）。
4. `test_services`：ingest 用 MockTransport 走通 发现→抓取→分块→落库；search_docs 命中插入的词并返回 snippet；
   jobs 提交任务后 versions/jobs 表状态流转。
5. 手动链路：`libref init-db && libref ingest docs https://docs.example.com && libref search 安装`；
   `libref serve-mcp` 后 MCP Inspector stdio 可列出四个工具。
6. `uv run pytest` 全绿（P0 测试不回归）。

## 4. 遗留任务（交付后核对，2026-11-07）

对照 §1–§3 逐项盘点：crawler 四件、chunking、document_store 六态、services 三件、
mcp_server/server、cli 均已交付并通过 48 条测试。以下为核对出的遗留项（含证据）：

| # | 事项 | 出处 | 现状（证据） | 处置建议 |
|---|---|---|---|---|
| L1 | `crawler/browser.py` Playwright JS 渲染降级 | tech-stack §4.2/§4.3（可选 extra，默认不装） | 未创建；pyproject `browser` extra 已声明 | 等真实 JS 站点需求再写，不阻塞 P2 |
| L2 | UrlSeeder BM25 URL 打分 + 发现结果磁盘缓存 | tech-stack §4.2「磁盘缓存 + HEAD 预筛 + BM25 URL 打分」 | HEAD 预筛已实现；`bm25s` 已安装但 src 内 0 引用；无磁盘缓存 | 大 sitemap 出现规模问题再补；短期可先移除僵尸依赖 |
| L3 | 「页面从 sitemap 消失」的 absent 判定缺失 | tech-stack §4.3 六态语义（蓝本 docs-mcp-server） | absent 仅在抓取返回 404/410 时触发（ingest.py:112）；发现集与已有 pages 做差集的"移除清理"没有，已删页面成僵尸行 | **建议修**：scrape 结束时 `已有 pages URL 集 − 本次发现集 → absent`，改动小、正确性影响实在 |
| L4 | scrape_docs 未接 job 队列 | module-layout §1 jobs「内存队列 + write-through」 | JobManager 模块与测试齐备，但 IngestService 全程同步，出口无提交入口 | **建议修**：services 包一层 `start_scrape_job()` + CLI `--async`，jobs 表 kind/params 协议已就绪 |
| L5 | `refresh_version` 独立入口 | module-layout §1 ingest 命名 | 功能上重跑 scrape_docs（etag/304 增量）已覆盖，缺意图化方法名与 CLI 子命令 | 可选：scrape 别名 + `libref refresh <库> <版本>` |
| L6 | serve-mcp stdio 真实握手未验证 | 本文 §3.5 | 单测覆盖 build_mcp_server 注册，未端到端起 stdio 客户端 | 补一条 mcp SDK stdio client 冒烟测试（不出网，成本低） |
| L7 | 真实站点 ingest 实测 | 本文 §3.5 | 链路仅 MockTransport 证明；CLI 冒烟只做了 init-db/search | 挑任意文档站 max_pages≤3 实测一次六态计数 |

说明：robots 的蓝本「MD5 变更检测」已由 TTL 到期重取并覆盖等价覆盖（MD5 存校验用途），不单列遗留。
优先级：L3 > L4 > L6 ≈ L7 > L5 > L2 > L1。
