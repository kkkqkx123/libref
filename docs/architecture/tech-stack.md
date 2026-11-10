# libref 技术栈与依赖设计

> 依据：`docs/req/init-req.md`（课题目标）与 `docs/ref/overview.md`（六项目源码级调查结论）。
> 结论先行：**Python + uv 单语言单体仓库，SQLite 作为唯一真源存储（元数据/正文/FTS），本地 simvec 引擎为默认向量后端（可重建派生索引，Qdrant 保留为可选后端），tree-sitter 做源码解析，自研爬虫（借鉴 crawl4ai 模式而非直接依赖）**。系统三层（挖掘构建 / 知识库检索 / Web+MCP 双出口）全部落在同一个 Python 包内，分阶段引入依赖。

---

## 1. 总体选型

| 层 | 选型 | 理由（对应对标项目核实结论） |
|---|---|---|
| 语言 / 包管理 | **Python ≥3.14 + uv**（uv 管依赖、虚拟环境、构建与脚本入口） | 全部核心借鉴对象（graphify、crawl4ai、EpiCoder、APP-Miner）均为 Python；docs-mcp-server 虽为 TS，但 init-req 明确"学习架构、Python 重写"。核心依赖（tree-sitter 0.26、tree-sitter-python 0.25 abi3）均已有兼容 3.14 的 wheel（PyPI 已核实）。 |
| 存储 | **SQLite（真源）+ simvec 本地向量（默认）/ Qdrant（保留）** | SQLite（sqlite3 标准库 + FTS5 全文）：docs-mcp-server 单机零外部依赖的架构被核实为可行且成熟，FTS5 内置 porter/unicode61 分词，承载全部元数据/正文/审核。Qdrant（qdrant-client 1.20）：sqlite-vec 无 ANN 索引（纯暴力扫描），库文档+库代码全量灌入后容量不足，已迁移至 Qdrant HNSW + payload 版本过滤（决策见 docs/plan/qdrant-migration.md）；Qdrant 定位为可重建派生索引，崩溃后按 SQLite 重灌，不做双写事务。 |
| AST 解析 | **tree-sitter（py-tree-sitter）+ 声明式 LanguageConfig** | graphify 已验证"声明式语言配置 + 通用抽取引擎"模式：新增语言只加配置不改引擎，且带文件 hash + grammar fingerprint 增量缓存。 |
| 文档抓取 | **自研爬虫：httpx + selectolax + 升级备选 Playwright**，借鉴 crawl4ai 的 URL 发现与正文净化算法 | 核实结论：crawl4ai 是通用爬虫框架，依赖重（自带 browser 池、LLM 模块），本课题只需其 robots→sitemap 链式发现、PruningContentFilter 正文净化、robots 缓存三个思路，自研实现量小且可控。 |
| 分块 | 自研两阶段分块（结构感知切分 + 贪心合并 + 代码围栏平衡） | docs-mcp-server 的 SemanticMarkdownSplitter / GreedySplitter 模式，纯逻辑无依赖。 |
| 序列挖掘 | **自研 PrefixSpan**（约 300 行，归一化整数索引输入），以 `ref/PrefixSpan-py` 为复刻蓝本（见 docs/ref/prefixspan.md） | PrefixSpan-py 为纯 Python、无重依赖的成熟极简实现，可直接复刻并定制（加权支持度、流式落库、closed 去冗余）；不引入 pyfim（2020 年停更、wheel 仅到 cp37 且无 abi3，非硬性依赖）。APP-Miner 仅借鉴其支持度阈值与 maximal 剪枝思路。 |
| 频次统计 | 自研（借鉴 EpiCoder `extract_frequency_count.py` 层级聚合 + top-N 剪枝、"结构树/频次表"分离存储） | EpiCoder 该部分为纯静态代码，可直接改写复用；其特征抽取靠 LLM，必须替换为 tree-sitter 静态抽取。 |
| Embedding | **主后端 SiliconFlow 免费 `BAAI/bge-m3`**（OpenAI 兼容 `/v1/embeddings`，1024 维；2026-11 用户决策，暂定策略），接口抽象预留多后端（本地 llama-server 等同协议端点可切换，`tools/hurl/` 已有示范） | 免本地权重分发，密钥经 `.env` 注入不入库；记录模型名+维度，变更时整体失效向量（借鉴 docs-mcp-server 的 metadata 校验机制）。 |
| MCP 出口 | **官方 `mcp` Python SDK**（FastMCP），stdio + Streamable HTTP 传输，令牌鉴权 | init-req 指定 MCP 官方 servers 为协议参考；Python SDK 的 FastMCP 覆盖 tools/resources 两类能力，与 docs-mcp-server 工具面（search_docs / read_page / list_libraries…）一一对应。 |
| Web 出口 | **FastAPI + uvicorn**，前端 **Vue 3 + Vite + Element Plus**（§7 决策点 1 已定） | docs-mcp-server 的三出口架构中 Web 层仅是服务层调用方；Python 侧 FastAPI 生态与 MCP SDK 的 HTTP transport 均基于 ASGI，可同进程部署。 |
| 数据校验 | **pydantic v2**（贯穿：抽取结果 schema、MCP 工具参数、API 模型） | graphify 的 validate.py 与 MCP SDK 的参数校验均需要统一 schema 层。 |
| 溯源与置信 | 自研字段：文档条目绑 URL；代码条目绑文件+**行范围**+实参列表；边带 EXTRACTED/INFERRED/AMBIGUOUS 置信标签 | overview 差异化创新点 3、4；graphify 的置信标签直接沿用作为审核优先级依据。 |
| 测试 | **pytest + pytest-asyncio**；检索质量基准（MRR/Recall@k）做成 CI 门禁 | docs-mcp-server 的检索基准模式被列为"重写时照搬防退化"。 |
| 任务队列 | **进程内 asyncio 队列 + write-through 到 versions 表 + 心跳恢复**，不引入 Celery/Redis | docs-mcp-server 核实结论：内存队列+版本行兼作任务记录已够用，单机系统引入外部队列是过度设计。 |

---

## 2. 项目布局（uv 标准布局）

```
libref/
├── pyproject.toml            # 唯一依赖声明处（uv 管理）
├── uv.lock
├── src/libref/
│   ├── config.py             # pydantic-settings 配置
│   ├── db/                   # schema、迁移、FTS/vec 触发器
│   ├── crawler/              # 分支 A：URL 发现、抓取、净化、robots
│   ├── chunking/             # 两阶段分块
│   ├── astminer/             # 分支 B：LanguageConfig、tree-sitter 引擎、增量缓存
│   ├── mining/               # 频次统计、共现、自研 PrefixSpan
│   ├── store/                # DocumentStore、RetrieverService、RRF 融合
│   ├── services/             # 业务工具层（MCP/Web/CLI 三出口共用）
│   ├── mcp_server/           # MCP 工具定义与传输
│   ├── web/                  # FastAPI 路由
│   └── cli.py
├── webui/                    # 前端独立工程（npm，不进 pyproject）
└── tests/
```

三出口（CLI / MCP / Web）**只调 `services/` 一层**——照搬 docs-mcp-server 的"业务工具类共用"模式，保证人机查询结果一致。

---

## 3. tree-sitter 集成方案（分支 B 核心）

### 3.1 依赖拆解

tree-sitter 在 Python 侧由三部分组成，需分别声明：

1. **运行时核心**：`tree-sitter`（py-tree-sitter，Language/Parser/Tree 绑定）。
2. **各语言 grammar wheel**：`tree-sitter-python`、`tree-sitter-java` 等，每个 wheel 内含编译好的 `.so` 语言库。**按需加语言，只加 wheel 不改引擎**。首个里程碑仅需 Python + Java（课题目标语言）。
3. **查询**：使用 tree-sitter query（S-expression）描述实体、调用、依赖、注释四类模式；语言配置保持最小（语言名、语法包、文件后缀、配置版本），节点类型声明位于查询文本内并纳入联合指纹，查询或语法变化即缓存失效。

### 3.2 LanguageConfig 设计（最小配置加表达式查询）

```python
@dataclass(frozen=True)
class LanguageConfig:
    name: str                          # 语言名，对应查询模块
    grammar: str                       # 对应 wheel 包名，动态 import
    file_suffixes: tuple[str, ...]     # 文件后缀集合
    config_version: int                # 配置版本，语义变化时加一
```

四类节点类型（类、函数、导入、调用）由各语言查询模块内的表达式文本声明，不在配置中重复。

抽取引擎（`astminer/engine.py`）产出**调用实例记录**，这是超越 graphify 的关键差异点：

```json
{
  "call_id": "sha1 确定性 ID（graphify ids.py 模式，含冲突盐）",
  "callee": "requests.get",           // 解析别名后的限定名
  "args": ["url", "params=params"],    // 实参列表（graphify 不存 → 本课题创新点）
  "caller_file": "rel/path.py",
  "line_range": [42, 48],              // 行范围（graphify 仅起始行 → 创新点）
  "confidence": "EXTRACTED",           // graphify 三级标签沿用
  "repo": "repo-id"
}
```

### 3.3 增量与批量

- 逐文件缓存：`file hash + grammar fingerprint + config schema 版本` 作为缓存键（graphify `cache.py` 模式），批量爬库时命中即复用。
- 跨文件解析两遍走：第一遍收声明/导入 facts（graphify `resolution.py` 模式），第二遍解析调用 → 置信标 EXTRACTED/INFERRED。
- Python 语言可用 stdlib `ast` 做交叉校验样例（tree-sitter 结果 vs `ast` 结果抽样 diff，进测试）。

### 3.4 版本钉住策略

grammar wheel 与 py-tree-sitter 存在 ABI 兼容窗口，`uv.lock` 钉死全部 grammar 版本；LanguageConfig 中记录 grammar fingerprint，升级 grammar 后缓存自动全量失效——这是 graphify 已验证的安全升级路径。

---

## 4. 爬虫系统集成方案（分支 A 核心）

### 4.1 决策：自研，不直接依赖 crawl4ai

理由（见 docs/ref/crawl4ai.md §7）：crawl4ai 面向通用 AI 爬取，自带 Playwright 浏览器池、LLM 抽取、深度爬取策略等大量本课题不需要的重依赖；而本课题需要的三个能力（robots→sitemap 链式发现、Pruning 正文净化、robots 缓存合规）算法简单，自研合计 <1k 行。**直接引入 crawl4ai 会拖入 Playwright 全家桶并放大安装失败面**（其 install.py 需要额外装浏览器）。

### 4.2 依赖分层

| 层 | 依赖 | 职责 |
|---|---|---|
| 抓取 | `httpx`（async，HTTP/2） | 静态文档站抓取；支持 If-None-Match/If-Modified-Since 增量 |
| 解析 | `selectolax`（lxml 绑定，快） | HTML→正文净化（Pruning 算法移植）→ Markdown（`markdownify`） |
| 反爬兜底 | `playwright` 为**可选 extra**（`uv add --optional browser`） | 默认不装；遇到 JS 渲染站点时按 docs-mcp-server 的 AutoDetectFetcher 模式降级 |
| robots | 自研（照搬 crawl4ai `utils.py::RobotsParser`：SQLite 缓存 7 天 TTL + MD5 变更检测） | 合规检查 + 站点限速（per-host backoff） |
| URL 发现 | 自研 sitemap 解析（支持 .gz / sitemap index / llms.txt 优先，crawl4ai AsyncUrlSeeder 模式） | 磁盘缓存 + HEAD 存活预筛 + BM25 URL 打分（`bm25s` 轻量库即可） |
| 仓库获取 | `dulwich`（纯 Python git，或 subprocess git） | 分支 B 拉取开源仓库；只 clone --depth 1 |

### 4.3 爬取管道（照搬 docs-mcp-server 三层结构）

```
发现层  UrlSeeder(robots→sitemap/llms.txt) → URL 队列
抓取层  HttpFetcher（+可选 BrowserFetcher 降级）→ per-host 退避
清洗层  中间件链：Normalize → SelectolaxParser → Defuddle 式正文提取
        → MetadataExtractor → LinkExtractor → Sanitize → HtmlToMarkdown
入队层  按 MIME 分派：Html / Markdown / SourceCode(进 tree-sitter 管道) / Text
```

增量语义照搬：pages 表存 etag/last_modified，六种页面结局（stored/unchanged/absent/empty/skipped/failed）。

---

## 5. 存储与检索

- **SQLite 表结构**直接以 docs-mcp-server 迁移目录（db/migrations 000~019）为事实标准用 Python 重写：`libraries / versions / pages / documents / documents_fts` + 本课题扩展表：
  - `api_entities`（确定性 ID、限定名、库版本归属）
  - `call_instances`（callee、args JSON、file、line_range、confidence）
  - `seq_patterns`（PrefixSpan 产出：pattern、support、来源实例引用）
  - `review_status`（审核工作流：pending/approved/conflict，挂每条知识条目）
  - `schema_meta`（键值元数据：vec_model/vec_dim，供向量索引失效校验）
- FTS5：`tokenize='porter unicode61'`，触发器同步（按列收窄的 update 触发器照搬）。
- 向量：**单集合**（`libref_chunks` 按库文件派生隔离），Point id=documents.id，payload 只存 `library_id`/`version_id` 过滤键，正文命中后回 SQLite 取；模型/维度变更时集合删除重建（元数据记于 schema_meta）。默认本地 simvec 引擎（进程内，Qdrant 语义的本地实现），Qdrant 服务保留为可选后端（`version_id` 整数索引 `is_principal=True` 租户优化）。
- 检索：FTS5 BM25（bm25(10.0,1.0,5.0,1.0) 权重）+ 向量 KNN（COSINE，version_id 过滤）两路 → **RRF 融合** → 频次特征重排（本课题创新：seq_patterns 支持度作为加权项）→ 段落重组（按 URL 分组扩展兄弟/父块）；向量不可用时降级为 FTS-only 单路。

---

## 6. pyproject.toml 依赖清单（分阶段落地）

### P0 基座（第一个 PR）

```toml
[project]
requires-python = ">=3.14"   # tree-sitter cp314 wheel / abi3 grammar wheel 均已核实兼容（2026-11）
dependencies = [
    "pydantic>=2.8",
    "pydantic-settings>=2.4",
    "qdrant-client>=1.20",             # 可选向量后端（保留，默认走本地 simvec，见 docs/plan/simvec-integration.md）
]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "ruff>=0.6", "mypy>=1.11"]
```

> 注：`requires-python` 维持 `>=3.14`。已核实（2026-11，PyPI JSON API）：tree-sitter 0.26.0 提供 cp314 wheel（全平台）；tree-sitter-python/java 等 grammar wheel 为 `cp310-abi3`（稳定 ABI，3.14 兼容）。均无版本兼容问题。（原 sqlite-vec 0.1.9 py3-none wheel 的兼容性核实已随向量层迁移 Qdrant 失效。）

### P1 分支 A（文档链路）

```toml
dependencies += [
    "httpx[http2]>=0.27",
    "selectolax>=0.3",
    "markdownify>=0.13",
    "bm25s>=0.2",
]
[project.optional-dependencies]
browser = ["playwright>=1.47"]      # 反爬兜底，默认不装
```

### P2 分支 B（源码挖掘链路）

```toml
dependencies += [
    "tree-sitter>=0.24",
    "tree-sitter-python>=0.23",
    "tree-sitter-java>=0.23",
    # 序列挖掘：自研 PrefixSpan，以 ref/PrefixSpan-py 为蓝本复刻定制（见 docs/ref/prefixspan.md），
    # 不引入 pyfim（2020 年停更、wheel 仅 cp27~cp37，非硬性依赖）
    "dulwich>=0.22",                   # git 只读拉取
]
```

### P3 双出口

```toml
dependencies += [
    "mcp>=1.2",                        # 官方 SDK，FastMCP
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    # Embedding 决策 2026-11：SiliconFlow bge-m3 API（OpenAI 兼容，复用 P1 的 httpx），
    # 不再引入 sentence-transformers——免本地权重分发
]
```

### P4 质量门禁

```toml
[dependency-groups] dev += ["pytest-cov", "hypothesis"]
```

---

## 7. 待用户决策点

1. **Web 前端** ✅ 已定（2026-11）：**Vue 3 + Vite + Element Plus**。
2. **Embedding 模型** ✅ 已定（2026-11，暂定策略）：**SiliconFlow 免费 `BAAI/bge-m3` API**
   （OpenAI 兼容 `/v1/embeddings`，1024 维，key 经 `.env` 注入）；接口抽象预留多后端，
   本地 llama-server 等同协议端点已示范可切（`tools/hurl/`）；
   SiliconFlow `/v1/rerank`（bge-reranker-v2-m3）留作检索精排可选档，不入 P3 验收。
3. **MCP 传输范围**：P3 先做 stdio（本地助手场景）还是 stdio+HTTP 同步上（需要令牌鉴权与 Origin/SSRF 校验）。默认建议先 stdio，HTTP 放 P3.5。
4. **PrefixSpan 对拍基准**：自研复刻（蓝本 ref/PrefixSpan-py）后，对拍基准用纯 Python 参考实现还是 README 黄金样例集。默认建议两者都用：README 示例做单元测试黄金样例，纯 Python 参考实现做随机数据对拍。

---

## 8. 落地顺序（与 overview.md §五 对齐）

1. P0：pyproject 改造 + db schema（含审核状态、溯源字段）；
2. P1：爬虫最小链路（sitemap→抓取→净化→分块→FTS）→ MCP `search_docs` stdio 原型；
3. P2：LanguageConfig(tree-sitter) → 调用实例抽取 → 层级频次 → 共现 → PrefixSpan；
4. P3：RRF+频次重排、HTTP MCP、Web 界面与审核工作流。
