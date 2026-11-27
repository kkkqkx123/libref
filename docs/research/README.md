# 图检索参考资料索引

本目录记录图遍历式代码检索、图 RAG 与独立图查询通道的源码级调研。资料来自本地只读参考仓库和 `docs/ref` 既有分析；不复制外部代码，不把参考仓库纳入构建。

| 文档 | 参考对象 | 关注点 |
|---|---|---|
| `graphify-graph-extraction.md` | `ref/graphify`、`docs/ref/graphify.md` | tree-sitter 抽取、确定性 ID、边置信、增量缓存、导出图 |
| `graph-rag-patterns.md` | `ref/llama_index` | 实体种子、有界邻域扩展、属性图检索、社区/关系文本组装 |
| `cce-independent-graph-path.md` | `code-context-engine` | 语义检索与关系检索并列，ego graph、path、components、export |
| `codebase-memory-graph-cache.md` | `ref/codebase-memory-mcp` | 大规模图的内存邻接缓存、类型掩码 BFS、有界查询 |
| `context7-availability.md` | `ctx7` CLI | 本轮工具可用性与结论来源说明 |

与本项目落地决策直接相关的设计见 `docs/plan/graph-traversal-query-channel.md`。
