# Crawl4AI 功能分析

> 仓库：https://github.com/unclecode/crawl4ai（本地 `../crawl4ai`）。Apache-2.0，Python。
> 定位：面向 LLM/AI Agent 的开源网页爬取与数据提取框架——"把网页变成适合 LLM 的干净 Markdown / 结构化 JSON"。本课题用于自研官方文档抓取模块。

## 1. 核心入口与抓取能力

- 核心入口：`crawl4ai/async_webcrawler.py::AsyncWebCrawler`，配合 `async_configs.py` 的 `BrowserConfig` / `CrawlerRunConfig`（另有 `DeepCrawlConfig`、`UrlSeederConfig`）完成"抓取 → 清洗 → Markdown/JSON 输出"。
- 抓取策略（`async_crawler_strategy.py`，策略模式）：
  - `AsyncPlaywrightCrawlerStrategy`（主力，Playwright 浏览器，支持 iframe、虚拟滚动、截图/PDF、hooks、会话）；
  - `AsyncHTTPCrawlerStrategy`（轻量 httpx，适合静态文档页）。
- 并发调度：`async_dispatcher.py`（信号量/限流，`arun_many` 批量爬取）；`browser_manager.py` 管理浏览器池。
- **深度爬取**（`deep_crawling/`）：BFS（`bfs_strategy.py`）、DFS（`dfs_strategy.py`）、最佳优先（`bff_strategy.py`），配合 `filters.py`（域名/URL/内容分数过滤）与 `scorers.py`（URL 打分排序，按相关性优先爬取，控制抓取预算）。

## 2. HTML 清洗与 Markdown 转换

- HTML→Markdown 引擎：`crawl4ai/html2text/__init__.py`（自带 `HTML2Text`）+ 定制子类 `CustomHTML2Text`（处理 pre/代码块）。
- Markdown 生成策略：`markdown_generation_strategy.py`——`MarkdownGenerationStrategy` 抽象 + 默认实现 `DefaultMarkdownGenerator`（`convert_links_to_citations` 链接转引用编号）。
- **正文净化（fit_markdown，文档页提取正文的关键）**：`content_filter_strategy.py`
  - `PruningContentFilter`：剪枝去导航/页脚/样板（lxml 高性能版 `PruningContentFilterLXML` 在 `content_filter_strategy_lxml.py`）；
  - `BM25ContentFilter`：按查询相关性保留段落；
  - `LLMContentFilter`（本课题不需要）。

## 3. Sitemap 解析与 URL 发现（最直接相关）

- `crawl4ai/async_url_seeder.py::AsyncUrlSeeder`：**robots.txt → sitemap 链式发现**（含 `.gz`、嵌套 sitemap index，`_iter_sitemap` / `_from_sitemaps`），磁盘缓存、HEAD 请求存活预筛、BM25 URL 相关性打分（`_calculate_bm25_score`）——即"抓官方文档站"第一步的成熟范式。
- `crawl4ai/domain_mapper.py::DomainMapper`：多源域内 URL 发现（sitemap、Common Crawl、Wayback、CT 日志、robots 路径挖掘、feed），兜底 sitemap 不全的情况。

## 4. robots.txt 遵守与爬取伦理

- `crawl4ai/utils.py::RobotsParser`（L282 起）：SQLite 缓存 robots 规则（7 天 TTL + MD5 变更检测），`can_fetch(url, user_agent)` 异步判定；爬取前由 `async_webcrawler.py`（L390）调用，开关为 `CrawlerRunConfig.check_robots_txt`。
- 深度爬取过滤器读 `meta robots` noindex（`deep_crawling/filters.py`）。
- 出口安全：`crawl4ai/egress_policy.py`（SSRF/内网防护）。

## 5. 结构化数据提取

- `extraction_strategy.py`：无 LLM 的 `JsonCssExtractionStrategy` / `JsonXPathExtractionStrategy`（schema 驱动）、`RegexExtractionStrategy`（内置正则）；LLM 的 `LLMExtractionStrategy`（本课题不需要）。
- 分块：`chunking_strategy.py`（topic/regex/sentence）；表格提取 `table_extraction.py`。

## 6. 对本课题的借鉴清单（文档抓取模块自研）

| 模块 | 借鉴点 |
|---|---|
| `async_url_seeder.py` | robots→sitemap 链式发现 + BM25 打分 + 磁盘缓存 + HEAD 预筛——URL 发现阶段直接参考 |
| `domain_mapper.py` | 多源 URL 发现兜底 |
| `content_filter_strategy.py` | Pruning/BM25 正文净化，文档页转干净 Markdown 的关键 |
| `markdown_generation_strategy.py` | 链接转引用、结构化 Markdown 输出格式 |
| `utils.py::RobotsParser` | 带缓存的 robots 合规检查，实现简洁可照搬 |
| `deep_crawling/bff_strategy.py` + `scorers.py` | 相关性优先爬取，控制文档站抓取深度与预算 |

## 7. 局限性

通用爬虫框架而非文档知识库：不解析源码、无版本索引、无检索、无 MCP/Web 出口；LLM 相关提取模块对本课题无用。作为**独立组件**仅借鉴其爬取/清洗/URL 发现实现。

## 论文相关工作表述

Crawl4AI 提供 sitemap 发现、HTML 正文净化与结构化 Markdown 转换的成熟 Python 实现，可支撑本课题官方文档抓取模块的自研；但其本身不涉及版本索引、代码挖掘或知识库服务。
