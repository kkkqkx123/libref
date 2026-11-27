# ctx7 可用性记录

本轮尝试使用 `ctx7` CLI 获取 Graphify 与 GraphRAG 的外部文档上下文：

```text
ctx7 whoami      → Not logged in
ctx7 library graphify "graph based code retrieval graph traversal" → fetch failed
ctx7 library microsoft/graphrag "graph rag" → fetch failed
```

当前部署环境没有可用登录态，外网请求失败，无法通过 Context7 MCP 拉取外部文档。因此 `docs/research/` 中的结论全部来自本地只读参考仓库（`ref/graphify`、`ref/llama_index`、`ref/codebase-memory-mcp`）、本地 cce 工作区与项目既有的 `docs/ref` 源码级分析。

这不是对外部工具结论的否定，只是本轮没有把无法验证的外部文档当作依据。若后续 Context7 可用，应补充核对 Graphify 官方 README、Microsoft GraphRAG 的 global/local search 文档，再更新对应研究笔记。
