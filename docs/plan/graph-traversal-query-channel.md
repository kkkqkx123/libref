# 图遍历式独立查询通道设计

## 结论

可以采用图遍历模式，但必须作为**独立查询通道**，不能并入现有 FTS/向量 RRF 排名，也不能复用文档特征注释的加权思路。

现有项目已经具备构建关系图所需的大部分事实表：`api_entities`、`call_instances`、`import_edges`、`feature_nodes`、`api_clusters`、`seq_patterns`、`doc_api_links`、`doc_feature_node_links`、`doc_link_edges`、`doc_pattern_coverage`。它们当前分别服务于源码挖掘、文档派生和探查统计，尚未组成统一的可查询图。新增通道应先建立只读逻辑图视图与有界遍历读模型，再向 CLI/MCP/Web 暴露独立工具。

## 参考模式

- Graphify：声明式语言配置、tree-sitter 抽取、确定性节点 ID、边置信标签（EXTRACTED / INFERRED / AMBIGUOUS）、跨文件解析。它的图主要用于导出与展示，查询能力弱，不适合直接照搬为检索服务。
- GraphRAG / LlamaIndex Property Graph：从查询抽取实体种子，按关系做有深度限制的邻域扩展，再把关系路径与原始文本一起交给答案组装。其 LLM 抽取三元组部分不适用于本项目；实体种子、有界扩展、社区上下文和组织证据的模式可以借鉴。
- cce：`crates/app/cce-orchestrator/src/query.rs` 明确区分语义检索器与 `RelationSearcher` / `GraphService`。后者提供 ego graph、shortest path、induced subgraph、connected components、export，并声明不参与 semantic scoring。这是本项目最贴近的目标形态。
- `codebase-memory-mcp/src/store/graph_mem.h`：大规模图上递归 SQL 会退化；把边一次性装入有界内存邻接结构，再执行有类型掩码的 BFS，是可参考的性能边界方案。

## 现有关系资产

| 事实 | 节点/边 | 可支撑的查询 |
|---|---|---|
| `api_entities` | API 实体节点；`qualified_name`、`kind`、`parent`、`bases`、文件与行范围 | 实体解析、继承关系、版本隔离 |
| `call_instances` | 调用边；`caller_entity_id` → `callee_entity_id`，带实参、行范围、`confidence`、`resolve_kind`、`relation_kind` | 调用链、邻域、影响面、调用点证据 |
| `import_edges` | 文件级别名导入边 | 跨文件绑定、依赖邻域 |
| `feature_nodes` / `api_clusters` | 层级节点与簇成员边 | 功能树上下文、社区式聚合视图 |
| `seq_patterns` | 频繁序列；相邻 API 可视为有序边，`support` 作为热度 | 常见调用顺序、用法配方 |
| `doc_api_links` | 文档块到 API 实体的边 | 文档反查实体、实体反查文档 |
| `doc_feature_node_links` | 文档块到特征节点 | 文档与功能树对齐 |
| `doc_pattern_coverage` / `doc_consistency` | 文档到模式覆盖及一致性判定 | 文档证据可信度、漂移提示 |
| `doc_link_edges` | 文档页到文档页 | 文档导航邻域、相关页面 |

强边口径保持保守：`doc_api_links.match_kind` 只有 `symbol` / `signature` 进入检索图；`context_symbol` / `name_fuzzy` 只保留诊断。调用边默认排除 `AMBIGUOUS`、被审核标为 `conflict` 的条目，以及 `stdlib` / `builtin` 之类非目标解析结果；显式要求时才展开。

## 通道边界

- 图查询返回节点、边、证据和路径，不与 `search_docs` 的文档结果混排。
- 不修改 `score`、`occurrences`、RRF 权重、rerank 输入或 top-N 注释位置。
- 图通道自身可以排序：按边置信、调用频次、模式支持度、路径长度和证据完整度排序；这是图内的结构顺序，不是文档相关性分数。
- 所有查询强制绑定 library / version / profile / tenant；版本不明时返回“不可测量”，不做跨版本拼接。
- 图谱边由现有事实表派生，不新建第二真相表；如后续需要缓存邻接结构，必须是可重建派生索引。

## 查询形态

1. 实体邻域：给定限定名或签名定位种子，按方向和边类型做 1 至 2 跳 BFS，返回调用者、被调用者、导入关系、相关文档与调用点证据。
2. 调用路径：给定起止实体，查找有界最短调用链；路径过滤在遍历阶段生效，而不是遍历后再裁剪。
3. 用法上下文：实体 → `call_instances` → 共现实体 / `seq_patterns` → `doc_api_links` 强边文档，输出结构化“常见配方”与对应证据。
4. 功能树上下文：实体 → `doc_feature_node_links` / `api_clusters` → 同簇实体与代表文档，作为 GraphRAG 社区摘要的确定性替身。
5. 影响面：反向遍历调用边，列出受影响实体与可追溯调用点；只展示结构影响，不推断行为等价。

## 逻辑图视图

先定义统一节点与边的读模型，不落库：

- 节点类型：`api`、`doc`、`feature`、`cluster`、`pattern`、`file`。
- 边类型：`call`、`import`、`inherit`、`mentions`、`covers_pattern`、`member`、`parent`、`doc_link`、`pattern_next`。
- 公共字段：`version_id`、`profile_id`、`tenant_id`、`confidence`、`evidence_ref`、`weight`、`source_table`。
- `weight` 只表达结构强度：调用次数、模式 support、强边置信；不得映射成文档相关性 `score`。

## 分层落地

- `store/`：提供只读图视图和有界 BFS / 最短路径原语；边遍历要有方向、类型掩码、深度、节点数和扇出上限。大规模数据可后续加进程内邻接缓存，缓存键包含库版本和事实表水位。
- `services/`：新增图查询编排，复用 `services/code/mining_read/` 的 `ApiUsageReader`、`PatternReader`、`FeatureReader`、`SymbolReader` 与现有证据渲染，不重复写频次、模式和文档读取 SQL。
- 出口：CLI、MCP、Web 只调用服务层，新增独立命令/工具/接口；不扩展 `search_docs` 的返回语义。
- 配置：只加入图遍历的操作边界参数，例如最大深度、最大节点数、允许边类型；不引入新的排序权重开关。

## 评测

- 不复用文档检索的 nDCG/MRR 增益基准；排序不变性由现有集成测试覆盖。
- 图通道需要单独的gold集合：实体种子、期望路径或邻域集合、允许边类型、证据出处。
- 指标为种子解析准确率、路径命中率、邻域精确率/召回率、证据可追溯率和查询尾延迟。
- 在gold集合与真实模型链路稳定前不写质量门禁，只做审核导出。

## 明确不做

- 不把 Graphify 的 `graph.json` 直接导入为另一套真相。
- 不引入 LLM 生成社区摘要或关系抽取；社区上下文来自 `feature_nodes`、`api_clusters`、`seq_patterns` 的确定性统计。
- 不把弱文档对齐边升级为强边，不把 `not_target` / `partial` / `ambiguous` 片段提升为目标调用证据。
- 不修改既有结果增强的“排序后 top-N 文本增强”边界。
- 不在本轮实现图数据库或持久化邻接表；SQLite 事实表足够启动，性能不足时再加派生缓存。

## 需要同步的既有说明

采纳该通道时，需同步 `docs/architecture/module-layout.md`、`docs/architecture/modules/store.md`、`docs/features/probe-and-scope.md` 中“链接边只读聚合，不做图遍历”的表述，并移除 `docs/plan/doc-feature-completion.md` 中“不实现 GraphRAG 式图谱聚合”的过期约束。

## 已落地实现

- `src/libref/store/graph.py`：只读逻辑图视图，支持实体解析、有界 BFS、最短路径、关系类型过滤、置信过滤与证据字段。
- `src/libref/services/graph_query.py`：图查询编排，复用 `MiningReadService` 提供用法上下文；不触碰检索分数。
- MCP `graph_query`、Web `GET /graph`、`librefctl graph` 三出口已接入同一服务；工具清单与 OpenAPI 契约同步。
- 配置新增 `graph_max_depth`、`graph_max_nodes`、`graph_max_fanout`、`graph_pattern_limit`，不新增排序权重。
- 集成测试覆盖强边、弱边、歧义边默认排除、调用路径、用法上下文、版本必填与 Web 出口。
- 调研资料归档在 `docs/research/`。
