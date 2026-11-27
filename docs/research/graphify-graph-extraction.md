# Graphify：tree-sitter 代码知识图谱

参考位置：`ref/graphify/`、`docs/ref/graphify.md`、`docs/ref/overview.md`。许可证 Apache-2.0，Python + tree-sitter，PyPI 包名 `graphifyy`。

## 定位

Graphify 把项目中的代码、Markdown、配置、PDF、图片等映射为可查询的 `graph.json`，目标是替代 grep 式浏览。代码侧完全使用 tree-sitter 静态解析，不依赖 LLM。

## 流水线

`detect() → extract() → build() → cluster() → analyze → report → export`。各阶段以字典或 NetworkX 图传递，最终产出 `graph.html`、`GRAPH_REPORT.md`、`graph.json`。

## 抽取设计

- `graphify/extractors/models.py::LanguageConfig`：每种语言只声明类、函数、导入、调用节点类型以及成员访问字段，新增语言不改通用引擎。
- `graphify/extractors/engine.py::_extract_generic` 与 `walk_calls`：通用调用遍历、成员调用、接收者类型推导，覆盖动态 `import()` / `require`。
- `graphify/extractors/resolution.py`：收集声明、导入、别名、使用 facts，支撑跨文件解析。
- 抽取对象包括类/接口/枚举/trait/record、函数/方法/构造器、导入、调用边、继承、类型引用。

## 图模型

节点字段含 `id`、`label`、`file_type`、`source_file`、`source_location`；边字段含 `source`、`target`、`relation`、`confidence`、`source_file`。关系类型包括 `calls`、`imports`、`uses`、`inherits`、`mixes_in`、`contains`、`references`、`method`。

边置信三级：

- `EXTRACTED`：源码显式存在。
- `INFERRED`：解析推导得到。
- `AMBIGUOUS`：无法消歧，供人工审核。

`graphify/ids.py::make_id` 与 `DeclIdRegistry` 生成确定性节点 ID；嵌套类 ID 包含父类 ID，冲突时加盐，保证增量构建身份稳定。

## 增量与缓存

`graphify/cache.py` 以文件 hash + tree-sitter grammar fingerprint（含 schema 版本）缓存逐文件解析结果。`graphify/build.py::merge_raw_extraction` 只替换变更文件子图，`graphify/affected.py` 计算受影响集合，`graphify/watch.py` 提供 debounce watch 模式。

## 对本项目的意义

可复用：声明式语言配置、确定性实体 ID、边置信标签、文件级增量缓存、跨文件两遍解析。不可直接复用：Graphify 没有调用频次、没有跨项目调用序列挖掘、溯源只到起始行、没有检索服务和 MCP/Web 出口。libref 的调用实例保存行范围、实参、频次和序列模式，属于对 Graphify 静态二元图的扩展。
