# crawler — 文档爬取层

## 职责

Branch A 的抓取前端：种子发现（llms.txt → sitemap → feed 链式回退）、robots 合规、增量抓取（etag/last-modified 再验证）、每 host 限速与退避、正文抽取与打分剪枝、HTML 代码块抽取、爬取范围策略，以及六生态包注册表解析与手工覆盖。借鉴 crawl4ai 的算法思路但完全自实现，不依赖 crawl4ai 框架。

## 关键文件

| 文件 | 职责 |
|---|---|
| `http.py` | 共享 httpx 客户端工厂（代理/NO_PROXY 挂载）、退避与 Retry-After 原语、`resilient_get` |
| `fetcher.py` | `HttpFetcher`：流式抓取、304 增量短路、每 host 串行 + 惩罚窗 |
| `robots.py` | `RobotsChecker`：robots 合规 + SQLite 缓存（TTL + MD5 完整性）、Sitemap 行解析 |
| `url_seeder.py` | `UrlSeeder`：llms.txt / sitemap / feed 三路种子发现 |
| `scope.py` | `ScopePolicy`、`score_url`、`crawl_key`、`BudgetExceeded` 等纯逻辑 |
| `extract.py` | `scrape_html` 正文抽取、复合打分剪枝、链接改写为编号引用 |
| `code_blocks.py` | `<pre>` 代码块抽取为 `CodeSnippet`，四路语言证据 |
| `browser.py` | Playwright 渲染回退（JS 页面） |
| `versioning.py` | 站点自述版本信号（URL 路径段多数投票） |
| `websearch.py` | DuckDuckGo HTML 搜索（结果只作未验证提示） |
| `github.py` | GitHub 仓库搜索 API 客户端 |
| `registry/` | 六生态注册表 provider + 手工覆盖表（见下） |

## 功能清单

- **抓取**：带 If-None-Match/If-Modified-Since 的条件 GET；304 → `unchanged`；404/410 → `absent`；体量双重守卫（Content-Length 预检 + 流式累计截断）。
- **限速**：每 host 一把 `asyncio.Lock`，锁内补足最小间隔（`crawl_delay`，夹在 [0, 30s]）；429/5xx 触发指数惩罚（`min(0.5·2^level, 8s)`，Retry-After 取较大者，加抖动），成功后清除。
- **robots**：`{scheme}://{host}` 为缓存键；不可达（404/网络错误）= 全部允许；缓存同时校验 TTL 与 MD5，不匹配即失效；`Sitemap:` 行按出现顺序去重解析。
- **正文抽取**（`scrape_html`）：元数据提取 → 相对链接绝对化 → 导航结构链接抢救（删除前从 `nav/[role=navigation]/.sidebar/.toc` 等抢救站内链接）→ 删除 `_STRIP_TAGS` → 选正文根（`main/article` 文本最长者，否则 `文本长度/节点数` 密度最高的容器）→ 复合打分剪枝 → 两轮空节点清理 → markdownify 转 Markdown。
- **复合打分**：`text_density = text_len/tag_len`、`link_density = 1 - link_len/text_len`、标签权重、负类名（nav/footer/ads/…）各 −0.5、`log(text_len+1)` 长度分，加权 `(0.4/0.2/0.2/0.1/0.1)` 归一到 0..1；`dynamic` 阈值对 article/main 等信息节点 ×0.8 宽容、链接堆 ×1.2 严苛。
- **剪枝实现要点**：selectolax 节点包装器无稳定身份，先一次前序遍历记录元素索引与父子关系，度量按索引缓存，再用栈自顶向下剪枝；`pre/code/table/img` 子树整体保留不深入（table 经 `is_data_table` 判定：有 `<th>`，或 ≥2 行且前 4 行中 ≥2 行单元格数 ≥2）。
- **JS 渲染回退**：静态抽取 <200 字符且 HTML 含 `__NEXT_DATA__`/`id="root"` 等标记 → 判为需渲染；`browser.render_html` 惰性启动 chromium，渲染 50 页回收防泄漏，删除 consent/overlay 节点并内联同源 iframe。
- **代码块抽取**：对每个 `<pre>` 收集四路语言证据（自身 class/data-lang、向上 4 层祖先、邻近标题文本、`tag.class` 选择器轨迹），交给 `astminer.language_evidence.resolve_language` 统一判定；在原 HTML 中 `find(raw_code)` 换算 UTF-8 字节偏移与行号，并提取前文 import 行作为 `import_context`。
- **版本信号**：对所有已爬 URL 的前 3 个路径段统计，`floor = max(2, ceil(0.8·页数))` 的多数命中且形如版本号/流名（latest/stable/dev…）的段为版本证据；与请求版本比较输出 `ok/warn/fail`。
- **搜索**：DDG HTML 端点须用浏览器 UA，解包 `uddg` 重定向参数；GitHub API 走 `api.github.com/search/repositories`，token 经 Bearer 头（不落存储）。

## 种子发现算法（UrlSeeder）

1. 进程内种子缓存（容量 256 FIFO，TTL 小时级，`force` 跳过）。
2. 链式回退：llms.txt 命中即用，否则 sitemap，再否则 feed；三路 attempted/errors 累计合并、保序去重。
3. **llms.txt**：从起始目录逐级向上生成候选层 × `llms.txt|llms-full.txt` 变体；给定 `path_prefixes` 时按前缀命中数选层，否则按 URL 数选层，得分 >0 当选。正文逐行解析 Markdown 列表项：显式 `](url)` 链接全部保留，裸 token 须是 http(s)/`/` 开头或形如文档路径才纳入。
4. **sitemap**：候选队列 = robots 声明的 Sitemap + 各目录层 `sitemap(.index).xml` 及 `.gz` 变体（魔数 `\x1f\x8b` 才解压）；`<sitemapindex>` 对每个 `<loc>` 子图递归，深度 ≥3 抛 `BudgetExceeded`；`<loc>` 用容错切分解析，不依赖 XML 库。
5. **feed 回退**：各目录 `feed.xml|atom.xml|rss.xml|feed`，四个容错正则（Atom link、RSS link、guid、enclosure）抽链接。
6. 结果逐 URL 清洗：`crawl_key` 去重（锚点类/跟踪类 query 参数全命中时归并为裸路径，fragment 永远剥除）→ 排除工具地址（robots.txt/ads.txt/`/.well-known/` 等）→ MIME 判定 → 跨站分流 → 可选 robots 与 HEAD 预检 → 相关度过滤（0..1 分数 = 命中 token 比例 × 路径深度惩罚）→ 超预算 `BudgetExceeded`。

## registry 子包

- **生态与镜像**：支持 pypi/npm/maven/go/crates/nuget 六生态，别名归一（py/pip→pypi 等）；镜像预设 `default`/`cn`（清华/npmmirror/阿里/goproxy.cn），解析顺序：显式 registry_bases > 镜像预设 > 内置默认表。
- **版本逻辑**：`_version_key` 按 `[.\-+_]` 切块数字优先排序（非 PEP 440/SemVer）；预发布判定按生态分派——npm/crates/nuget 连字符尾巴即预发布，pypi 额外吃 PEP 440 尾（a/b/rc/dev…），maven 追加 m/ea/cr/milestone，go 吃伪版本形（`v\d+.\d+.\d+-\d{14}-hex12`）；token 提示用词边界匹配（接受 `beta1`，拒绝 `compressed` 中的子串 `pre`）。
- **各 provider 差异**：
  - pypi：PEP 503 名字归一；剔除全 yanked 版本；docs 候选按 project_urls 键名（documentation → changelog → home）分级排名。
  - npm：`dist-tags.latest` 为 stable；scope 包名原样。
  - go：模块路径大写转义为 `!` + 小写（module proxy 规范）；`@latest` + `@v/list` 双请求；pkg.go.dev 为唯一 docs 候选。
  - maven：坐标保留大小写；`maven-metadata.xml`（namespace 容忍，退化元数据回退全文 `<version>`）+ POM 项目根 `<url>`（子元素不得压过）；候选追加 javadoc.io。
  - crates：剔除 yanked；docs.rs 为回退候选。
  - nuget：V3 registration 索引可能分页（至多追 2 页）；`listed:false` 剔除；官方 base 下再解析 nuspec 补 repo URL，失败不阻塞。
- **手工覆盖**：`overrides.json`（内置种子，当前为空数组）+ 外部文件双层（文件层优先），按 `(生态, 规范化名)` 精确匹配压过注册表推断（仅让位于用户显式 URL）；`verified_at` 超 TTL 列入过期清单供 CI 门禁，不自动降级。
- **SSRF 防护**：`sanitize_http_url` 默认拒私有/回环主机（localhost、`.local/.internal`、RFC1918 段），去 `git+` 前缀、`git://` 升级 https。

## 依赖与约束

网络请求全部经注入的 `httpx.AsyncClient`（便于 MockTransport 测试）；`robots.py` 是唯一直接写 SQLite 的点（`robots_cache` 表）。语言证据判定委托 `astminer.language_evidence`（纯函数）；被 services（ingest/discovery/probe）消费。
