# chunking — 结构化分块

## 职责

把文档 Markdown 按标题路径切分为可索引的 chunk：双路径预算（BM25 词项 / embedding token）、围栏感知的成本切分、贪婪合并碎片、前块 overlap 附加，以及文档分面（facet）聚合。纯内存算法，不触数据库。

## 关键文件

| 文件 | 职责 |
|---|---|
| `budget.py` | `ChunkBudget`、`ChunkPath`（BM25/EMBEDDING 双路径）、token/词项计量与截断 |
| `splitter.py` | `split_markdown_dual` 主流程：标题切段 → 双路径切分 → 去重 → overlap |
| `doc_facets.py` | 12 个固定分面的聚合（block_type、code_ratio、param_names 等） |

## 功能清单

### 双路径预算（budget.py）

同一份 section 按两套计量独立切分：

- **EMBEDDING 路径**：`estimate_tokens` 按字符类别加权——ASCII 字母/数字/下划线 0.25、ASCII 空白 0.2、其余（含 CJK）1.0，向上取整。权重按配置 embedding 模型实测校准；更换模型须重新校准。
- **BM25 路径**：`count_terms`，`[A-Za-z0-9_]+` 连续串算 1 词项 + CJK 每字算 1（有意保守，镜像 FTS 对 CJK 的逐字展开）。
- `ChunkBudget(hard_limit, min_limit, overlap, reserve_ratio=0.06)`：`body_limit = hard_limit − int(hard_limit·0.06)`，扣掉标题头等装饰预留，不动硬限。
- `truncate_to_cost` 用二分搜索最长合法前缀（字符边界内 cost ≤ limit）。

### 切分算法（splitter.py）

`split_markdown_dual` 流程：

1. `_split_by_headings` 逐行切段，维护**标题路径** `dict[级别→标题]`——当前级别写入并删除所有更深层级，路径串为 `一级 / 二级 / 三级`；围栏内（` ``` `/`~~~` 翻转状态）的 `#` 行不是标题；标题文本剥掉行内链接（`## [Coercion](url)` → `Coercion`）。
2. BM25 与 EMBEDDING 两条路径各自独立执行 `_split_path`（同一输入、不同预算）。
3. 双流去重：以 embedding 流内容集为基准，BM25 流中的孪生块丢弃（同一内容只在 embedding 路径保留一次）。
4. 各流重排序号、重接 prev/next、按本路径计量截取前块尾部 overlap 附加。
5. 末尾断言 `assert all(_fences_balanced(...))`——围栏平衡不变量。

**贪婪合并**：仅当前段与本段标题路径相同、前段 cost < `min_limit`（太碎）、且合并后 ≤ `body_limit` 时并入，合并记录 `merged_from`。

**成本切块**：整段超限则贪心逐行组装（缓冲非空、本行非围栏行、不在围栏内、加行超限即封口）；单行超限先按句边界 `(?<=[.!?;。？！；])\s+` 切，句子放不下再字符预算硬切。

**围栏感知切分**（核心不变量）：段内可能携带超过 body 预算的代码块，直接切会切开围栏。逐行组装时记住当前打开的围栏 marker（前三字符）；当前行成本（含围栏内需加封口 marker 成本）超限时——不在围栏内正常封口；**在围栏内则合成边界**：块尾追加 marker（关闭）、新块以 marker 开头（重开），代码体逐字保留。

**embedding 路径装饰复查**：每块会加 `title / path` 头行，规划时保守扣除头成本；"块+头"仍超硬限的块用剩余空间重新围栏感知切分。`ensure_hard_limit` 是装饰附加后的最终校验。

### 关键词与内容指纹

- `extract_keywords`：拉丁词（≥3 字符）整词计数 + CJK 2-4 字窗口计数，过滤中英停用词取 top-N；每个词追加 `ident_snake_form` 蛇形变体（跨命名风格检索）。
- `content_hash = sha256(title\0path\0content)`：增量复用键，store 据此跳过重嵌入。

### 文档分面（doc_facets.py）

`aggregate_doc_facets` 对每块一次性产出 12 个固定顺序分面：`block_type`、`heading_depth`、`length_bucket`、`code_ratio`、`has_table`、`deprecated`、`version_marker`、`example_language`、`example_symbols`、`param_names`、`return_hint`、`term_candidates`。要点：

- `_block_type` 判定序：changelog → warning → params（有表且含参数词）→ example（有围栏）→ narrative；词表中英混排（deprecated/废弃、warning/注意 等）。
- 围栏解析是容错状态机：配对同字符且长度 ≥ 开栏才闭合；`code_hash` = 代码体空白归一化后 sha256（代码片段身份，对重排空白不敏感）。
- `param_names` 识别 Markdown 参数表：表头首格命中 `param|argument|参数|^name$` 即启动收集，跳过 `---` 分隔行，取首列首词。
- 空值剪除：非六个"固有分面"（block_type/depth/length/ratio/table/deprecated）的空值行跳过；每块最多保留 `candidate_budget`（默认 200）条分面行。

## 对外接口

`split_markdown` / `split_markdown_dual`、`Chunk`、`DualChunks`、`ChunkBudget`、`budgets_from_settings`、`aggregate_doc_facets`、`clean_text_for_bm25`（转口 `libref.text.bm25`）、`chunk_content_hash`。

## 依赖与约束

依赖 `text`（清洗/蛇形变体）；预算默认值来自 `LibrefSettings` 的 chunk_* 配置。分块产物由 services 写入 db（documents 表），`content_hash` 是增量再验证的第二道短路（服务器未返回 304 时按指纹判 unchanged）。围栏平衡与 `text.fences_balanced` 共用同一不变量。
