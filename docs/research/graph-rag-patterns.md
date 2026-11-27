# 图 RAG：实体种子、有界扩展与关系证据

参考位置：`ref/llama_index/llama-index-core/llama_index/core/indices/knowledge_graph/retrievers.py`、`indices/property_graph/base.py`、`indices/property_graph/sub_retrievers/`。

## 典型链路

图 RAG 的检索通常不是把整张图塞给模型，而是：

1. 从查询中抽取关键词或实体种子。
2. 在属性图或知识图谱中按种子做有深度上限的邻域扩展。
3. 把关系三元组、原始文本块和种子命中的文档一起组装成答案材料。
4. 对关系文本去重、截断，并保留来源节点。

## LlamaIndex 的具体形态

- `KGTableRetriever` 支持 `keyword`、`embedding`、`hybrid` 三种种子检索模式；`graph_store_query_depth` 限制图查询深度；`max_knowledge_sequence` 限制关系文本数量。
- `use_global_node_triplets` 可从命中的文本块再抽取关键词，扩展种子集合；这是“局部命中带动全局上下文”的可选开关。
- `PropertyGraphIndex` 把三元组抽取拆成可插拔 transformation；`sub_retrievers` 提供向量、Cypher 模板、LLM 同义词等子检索器。
- 关系过滤发生在图查询阶段，单纯在结果上裁剪会破坏路径语义。

## 与本项目的取舍

可借鉴：

- 实体种子 → 有界邻域 → 原文证据的两段式结构。
- 路径和邻域都保留来源，便于人工审核。
- 深度、序列数、扇出都要有硬上限。

不适合照搬：

- LLM 抽取实体关系会把幻觉引入知识库；libref 的关系应来自 `call_instances`、`import_edges`、`doc_api_links`、`seq_patterns` 等确定性事实。
- LLM 生成社区摘要缺少可追溯证据；libref 应使用 `api_clusters`、`feature_nodes`、`seq_patterns` 的统计结果作为社区上下文。
- 图关系不应混入现有 RRF 排名；应作为独立通道输出节点、边和证据。
