# 对标项目功能分析汇总（docs/ref）

> 本目录是对六个对标项目**源码级调查**的分析文档，逐篇见同目录各文件：
> `docs-mcp-server.md`、`crawl4ai.md`、`graphify.md`、`epicoder.md`、`app-miner.md`、`prefixspan.md`。
> 结论基于对本地仓库（`ref/<项目名>`）的实际代码核实，与 `docs/req/init-req.md` 中的调研描述有出入之处已在各篇中标出。

## 一、项目对照总表

| 项目 | 语言/许可 | 核心实现 | 数据源 | 版本隔离 | 频次/序列挖掘 | 溯源 | MCP | Web UI |
|---|---|---|---|---|---|---|---|---|
| docs-mcp-server | TS / MIT | 文档爬取+分块+SQLite FTS/向量检索 | 官方文档站/GitHub/npm/PyPI | ✅ 库/版本分区索引 | ❌ | ✅ URL+块路径 | ✅ stdio/HTTP/SSE | ✅ 完整管理界面 |
| crawl4ai | Python / Apache-2.0 | sitemap 发现+正文净化+Markdown 转换 | 任意网页 | ❌ | ❌ | — | ❌ | ❌ |
| graphify | Python / Apache-2.0 | tree-sitter AST→代码知识图谱 | 源码 | ❌ | ❌（二元调用边，无频次） | ✅ 文件+起始行号 | ❌ | ❌（仅导出 HTML/图） |
| EpiCoder | Python / MIT | LLM 特征抽取+Feature Tree+层级频次统计 | The Stack V2 | ❌ | ⚠️ 仅单文件特征频次，无共现/序列 | ❌ | ❌ | ❌ |
| APP-Miner | Python/C++/C | LLVM IR 数据流分析→位集 Apriori 频繁模式挖掘 | C 项目源码 | ❌ | ✅ 频繁图模式（非序列） | ✅ 文件+函数 | ❌ | ❌（文本报告） |
| **本课题（libref）** | Python | 官方文档+源码挖掘双路 → 知识库+检索 → Web+MCP 双出口 | ①官方文档 ②海量开源代码 | ✅ | ✅ 共现频次+PrefixSpan 序列 | ✅ URL / 文件+行范围 | ✅ 令牌鉴权 | ✅ 含审核工作流 |

## 二、各项目对本课题的贡献分工

| 课题层 | 首要参考 | 借鉴要点 |
|---|---|---|
| 文档抓取（分支 A） | crawl4ai + docs-mcp-server | sitemap/robots 发现（AsyncUrlSeeder）、正文净化（PruningContentFilter）、中间件链清洗、llms.txt |
| 版本索引与存储 | docs-mcp-server | libraries/versions/pages/documents 表结构、FTS5+vec0 分区、增量抓取（etag/last_modified）、任务状态 write-through |
| 分块 | docs-mcp-server | 标题路径 section.path 结构感知切分 + 贪心合并 + 代码围栏平衡 |
| AST 抽取（分支 B） | graphify | LanguageConfig 声明式语言配置、walk_calls 调用抽取、接收者类型推导、确定性 ID、逐文件增量缓存 |
| 特征抽取与频次 | EpiCoder | 层级频次聚合 + top-N 剪枝（extract_frequency_count.py）、树/频次分离存储（utils/tree.py）——注意其特征抽取靠 LLM，须改纯静态 AST |
| 序列挖掘 | APP-Miner + PyFIM | 调用上下文归一化为整数索引、支持度阈值 max(置信度, 最小支持度)、maximal 模式剪枝；算法本体用 PrefixSpan |
| 检索 | docs-mcp-server | RRF 混合检索、结果段落重组、检索质量基准（MRR/Recall@k） |
| MCP 出口 | docs-mcp-server + MCP 官方 servers | 工具参数设计（search_docs/read_page 等）、stdio/HTTP 传输、只读裁剪、令牌鉴权 |
| Web 出口 | docs-mcp-server | 库/版本管理、任务进度、分块浏览、在线检索；本课题另加审核/冲突工作流（DeepWiki 溯源思路） |

## 三、与初始调研（init-req.md）不符的关键事实（论文写作需注意）

1. **APP-Miner**：不是 Android APP 分析，也不使用 PrefixSpan/GSP——它针对 C 项目（Linux 内核/OpenSSL 等），做 LLVM IR 数据流分析上的**频繁 API 路径图模式**挖掘（位集 Apriori 自研算法，maximal 剪枝）。论文相关工作段落中不应写成"序列挖掘基线"，可表述为"基于静态分析的频繁 API 模式挖掘"；本课题的序列挖掘算法（PrefixSpan）来自 PyFIM/自研。
2. **EpiCoder**：特征抽取**完全依赖 GPT-4 而非 AST**（仓库中无 tree-sitter/ast 解析），可复用的是其频次统计、剪枝、树操作等纯静态模块；直接借鉴其"特征抽取"时必须改造为纯静态实现，以免引入 LLM 二次幻觉。
3. **Graphify**：调用边不带实参且溯源仅起始行号（非行范围），无频次计数——这两点正是本课题抽取层可超越的设计点（存行范围+实参列表+调用次数）。
4. **docs-mcp-server**：实现远比调研描述丰富——双出口实为 CLI/MCP/Web 三出口，分块为两阶段（语义切分+贪心合并），检索融合用 RRF，值得整表照搬其 schema。

## 四、本课题相对各方的差异化（创新点落点）

1. **双数据源融合**：官方文档（Grounded Docs 路）+ 源码挖掘（Graphify/EpiCoder 路），任一对标项目都只有单一路径。
2. **序列级挖掘**：在 graphify 式调用实例之上做共现频次统计与 PrefixSpan 序列模式挖掘（各对标均缺失）。
3. **更细的溯源粒度**：文档条目绑 URL，代码挖掘条目绑文件+行范围+实参（DeepWiki 溯源思路 + 超越 graphify 的粒度）。
4. **人工审核/冲突工作流**：所有对标均无；可复用 graphify 边置信标签（EXTRACTED/INFERRED/AMBIGUOUS）作为审核优先级依据。
5. **人机双出口共享同一知识库**：MCP 工具与 Web 查询走同一服务层（照搬 docs-mcp-server 的业务工具类共用模式）。

## 五、libref 当前状态与下一步

当前为纯文档立项阶段（仅 `docs/req/init-req.md` + 本 ref 目录），零代码。建议落地顺序：

1. 依 docs-mcp-server schema 用 Python 定义 SQLite 表结构（含审核状态、溯源字段）；
2. 实现分支 A 最小链路：爬虫（借 crawl4ai 模式）→ 分块 → SQLite FTS 索引 → MCP `search_docs` 原型；
3. 实现分支 B：tree-sitter LanguageConfig（借 graphify）→ 调用实例抽取（文件+行范围+实参）→ 共现频次（借 EpiCoder 统计模块）→ PrefixSpan；
4. 检索加 RRF + 频次重排；Web 界面与审核工作流最后叠加。
