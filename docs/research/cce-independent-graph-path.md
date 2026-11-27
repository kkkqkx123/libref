# cce：与语义检索并列的独立图查询路径

参考位置：`code-context-engine/crates/app/cce-orchestrator/src/query.rs`、`query/graph/model.rs`、`query/graph/service.rs`、`query/relation_searcher.rs`。

## 架构边界

`query.rs` 把查询层分成两条并列路径：

- `Searcher`：Dense / BM25 / Summary 召回、融合、过滤、归一化、rerank、阈值过滤，负责语义相关性。
- `RelationSearcher` / `GraphService`：调用链查询、路径查找、继承关系、ego graph、shortest path、components、export；明确不参与 semantic scoring。

注释后处理发生在排序和阈值过滤之后的 top-N，只加结构标记，不改变顺序。这与 libref 当前“结果增强不参与排序”的口径一致。

## 图数据模型

`graph/model.rs` 定义扁平节点列表 + 扁平边列表：

- 节点含稳定 ID、可读标签、类型、源文件、源码位置、可选 scoped name 和 signature。
- 边含 source、target、relation、domain、confidence、call_context、is_external、weight、cfg_condition、callsite、edge_id。
- `confidence_of()` 把外部引用标为 `EXTERNAL`，直接调用标为 `EXTRACTED`，其他推导关系标为 `INFERRED`。
- 边身份用 `(source, target, relation[, callsite])` 的内容摘要；调用点参与哈希，区分同一实体对上的多次调用。

## 服务操作

`graph/service.rs` 提供：

- `ego_graph(root, depth, direction)`：有界 BFS，方向可为 forward / backward / both。
- `shortest_path(start, end, max_depth, filter)`：路径过滤在遍历阶段生效。
- `subgraph(ids, filter, pagination)`：显式节点集合上的诱导子图。
- `connected_components(filter, pagination)`：按过滤后的内部边做并查集。
- 单图操作有 `MAX_GRAPH_NODES` 上限；分页与过滤是模型的一部分。

## 对本项目的意义

libref 的独立图通道应照搬这个边界，而不是照搬实现语言或存储：图查询返回节点、边、证据和路径，不与 `search_docs` 混排；结构排序只在图通道内部生效；所有边必须绑定事实表来源与置信标签。
