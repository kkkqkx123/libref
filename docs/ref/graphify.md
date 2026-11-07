# Graphify 功能分析

> 仓库：Graphify-Labs/graphify，Apache-2.0，Python 3.10+（PyPI 包名 `graphifyy`）。
> 定位：把整个项目（代码、Markdown、PDF、图片等）映射为一张可查询的代码知识图谱（`graph.json`），替代 grep。代码部分**纯 tree-sitter 本地解析、无 LLM**。

## 1. 流水线与输入

- 流水线（`ARCHITECTURE.md`）：`detect() → extract() → build() → cluster() → analyze → report → export`，各阶段以 dict / NetworkX 图传递。
- 输入：约 40 种编程语言（Python、Java、JS/TS、Go、Rust、C/C++、C#、Kotlin、Ruby、PHP、Swift 等）；Markdown/注释 rationale；配置（JSON/YAML/Terraform/SQL）；文档/媒体（PDF、Office、图片）。文件分类为 `code / document / paper / image / rationale / concept`（`graphify/detect.py`、`graphify/validate.py`）。

## 2. tree-sitter AST 抽取

- **声明式语言配置（最值得抄的设计）**：`graphify/extractors/models.py` 的 `LanguageConfig` 数据类，每种语言只声明 AST 节点类型：`class_types` / `function_types` / `import_types` / `call_types`，及 call 的字段名（`call_function_field`）、成员访问节点类型（`call_accessor_node_types`）等；各语言实例见 `graphify/extract.py`。
- **通用抽取引擎**：`graphify/extractors/engine.py` 的 `_extract_generic` + `walk_calls`，处理 callee 名、成员调用、接收者类型推导（Python/Java/TS/C#/Go 有 `*_receiver_types` 第二遍跨文件解析），动态 `import()` / `require` 也覆盖。语言专属钩子 `extra_walk_fn`。
- **符号解析**：`graphify/extractors/resolution.py` 收集声明/导入/别名/使用 facts，支撑跨文件调用解析。
- 抽取实体：类（含 interface/enum/trait/record）、函数/方法/构造器、导入、**调用边**、继承（extends/implements）、类型引用。

## 3. 图数据模型（溯源机制）

```json
// 节点
{"id": "唯一串", "label": "人读名", "file_type": "code",
 "source_file": "相对路径", "source_location": "L42"}
// 边
{"source": "...", "target": "...",
 "relation": "calls|imports|uses|inherits|mixes_in|contains|references|method",
 "confidence": "EXTRACTED|INFERRED|AMBIGUOUS", "source_file": "..."}
```

- 节点类型：文件、类/类型、函数/方法、外部 stub（`_mint_external_stub`）、语义节点。
- **每条边带置信标签**：EXTRACTED（源码显式）/ INFERRED（解析推导）/ AMBIGUOUS（待人审，报告中标记）——与课题"人工审核冲突条目"需求天然契合。
- 溯源粒度：`source_file`（相对 scan root）+ `source_location`（**仅起始行 "L42"，非行范围**）；边带调用发生文件。字节级精确 span 用于内部解析但不导出。
- 确定性 ID：`graphify/ids.py`——`make_id` / 冲突 sha1 盐 / `DeclIdRegistry`，嵌套类 ID 包含父类 ID，保证增量构建时身份稳定。

## 4. 存储与导出

- 存储：`graphify/export.py::to_json` → `graphify-out/graph.json`（Node-Link JSON）。
- 导出：`to_json / to_html`（力导向交互图）/ `to_obsidian / to_svg / to_graphml / to_canvas / to_cypher`（图数据库导入）；`graphify/callflow_html.py` 生成 **Mermaid** 调用流 HTML。

## 5. 增量解析

- 逐文件 AST 缓存：`graphify/cache.py`——按**文件 hash + tree-sitter grammar fingerprint**（含 schema 版本号）缓存，stat 索引加速变更检测。
- 增量合并：`graphify/build.py::merge_raw_extraction` 只替换变更文件的子图，保留语义节点证据；`graphify/affected.py` 计算受影响集合；`graphify/watch.py` watch 模式（debounce 3s）。
- 多仓库/monorepo：`graphify/global_graph.py`、`cross_repo_calls.py`（跨仓库链接解析，非序列挖掘）。

## 6. 批量解析入口与流程

CLI `graphify/cli.py::dispatch_command`：`detect(root)` 分类 → `collect_files()` 展开 → 逐文件派发到语言 extractor（缓存命中复用）→ `build()` 生成 NetworkX 图 → Leiden 社区划分（`cluster.py`）→ `analyze.py`（god nodes 等）→ `report.py` 生成 `GRAPH_REPORT.md` → 导出三件套（graph.html / GRAPH_REPORT.md / graph.json）。

## 7. 对本课题的借鉴清单

| 文件 | 借鉴点 |
|---|---|
| `extractors/models.py` | LanguageConfig 声明式配置：新增语言只加配置不改引擎 |
| `extractors/engine.py` | walk_calls 通用调用抽取、接收者类型推导、跨文件第二遍解析 |
| `ids.py` | 确定性节点 ID + 冲突盐——API 实体跨仓库去重身份的关键 |
| `cache.py` | 文件 hash + grammar fingerprint 的增量缓存（批量爬库必备） |
| `validate.py` | 抽取结果 schema 校验（节点/边必需字段含 source_location） |
| `build.py` | 抽取 dict → 图、跨文件端点 resolve |
| `export.py` | graph.json / GraphML / Cypher 导出模板 |

## 8. 局限性（与本仓库代码核实）

- **不做调用频次统计**：调用边是二元的，无次数/频率计数，无基于频次的排序。
- **不做跨项目 API 调用序列挖掘**：边无序、不带实参；无 PrefixSpan/GSP/n-gram 类算法。
- **溯源粒度粗**：仅起始行号，无列号/字节范围/实参上下文——本课题可超越点（存行范围 + 实参列表）。
- 无动态/运行时分析（反射、动态分发明确不推断）；无嵌入/向量检索；无 MCP 服务和 Web 查询界面；不抓取官方文档。

## 论文相关工作表述

Graphify 基于 tree-sitter 构建代码知识图谱，通过声明式语言配置从源码抽取类、函数、导入与调用关系，并为每个实体绑定源码位置实现溯源；但仅保留静态二元调用结构，缺少调用频次统计、参数级信息与跨项目的 API 调用序列模式挖掘能力。
