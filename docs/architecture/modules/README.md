# modules — 模块文档索引

本目录按包逐一描述 `src/libref` 与 `librefctl/src` 的实现：职责边界、完整功能清单、算法与复杂逻辑的具体实现、对外接口与依赖约束。包名与 `docs/architecture/module-layout.md` 的分层一一对应。

## 分层总览

```
crawler ─┐
chunking ├→ db ←─ store ←─ services ←─ {cli, mcp_server, web}
astminer ┘   ↑       ↑
mining ──────┘       llm (transport leaf, 注入消费)
```

| 文档 | 包 | 一句话定位 |
|---|---|---|
| [astminer.md](astminer.md) | `astminer/` | tree-sitter AST 抽取：声明/别名/调用三类事实 + 两遍跨文件调用解析 |
| [crawler.md](crawler.md) | `crawler/` | 文档爬取：种子发现、增量抓取、正文抽取、六生态注册表解析 |
| [chunking.md](chunking.md) | `chunking/` | 标题路径分块：双路径预算、围栏感知切分、文档分面 |
| [mining.md](mining.md) | `mining/` | 统计与序列挖掘：PrefixSpan 闭包模式、聚类、切面、文档对齐 |
| [db.md](db.md) | `db/` | 最底层 SQL：SQLite/PostgreSQL 双栈、37 步迁移、后端契约 |
| [store.md](store.md) | `store/` | 真源持久化 + 混合检索：FTS5/向量双路、RRF 融合、向量集合治理 |
| [llm.md](llm.md) | `llm/` | 模型传输叶子：embed/rerank/chat、重试与共享韧性状态 |
| [text.md](text.md) | `text/` | 共享文本变换叶子：CJK 展开、snake 对齐、摘录标记 |
| [services.md](services.md) | `services/` | 唯一编排层：仓库索引管线、挖掘刷新、文档摄取、答案链路、作业队列 |
| [cli.md](cli.md) | `cli/` | 本地命令行：服务启动 + 本机索引/挖掘/运维 |
| [mcp_server.md](mcp_server.md) | `mcp_server/` | 27 个 MCP 工具 + jsonschema 参数契约中间件 |
| [web.md](web.md) | `web/` | FastAPI REST（约 35 端点）+ SPA 壳 + 认证/配额中间件 |
| [librefctl.md](librefctl.md) | `librefctl/` | 独立的 HTTP 客户端包，声明式命令表对接 web 路由 |

## 横切约定

- **依赖方向**：单向、分层收敛；db 是唯一共享 SQL 底层；services 是唯一编排层；三个出口都是薄出口。
- **真源与派生**：SQLite（或 PostgreSQL）是唯一真源；向量索引是可重建派生物，仅 `store/vec_store` 持有引擎句柄。
- **边界 schema**：跨包记录（抽取记录、调用实例、MCP 参数、REST 响应）全部 pydantic，单一定义无字段漂移（MCP 工具入参例外，走 jsonschema 契约中间件）。
- **确定性**：挖掘全链路排序带名字序决胜键，同一输入必得同一输出；`content_hash`/`pattern_hash`/`entity_uid` 等语义键哈希支撑幂等 upsert。
- **恢复语义**：索引与摄取管线均为 sync-then-mark——派生索引同步完成后才推进断点游标，崩溃即重放该批。
- **纯内存算法**：`mining/` 与 AST 抽取核心零 db 依赖；`text/` 零依赖叶子；持久化一律由 services 编排。
