# mcp_server — MCP 工具出口

## 职责

Model Context Protocol 工具出口：把检索、发现、挖掘读模型、运维与索引请求暴露为 MCP 工具，供外部 AI 助手调用。薄出口层——工具函数只做 `ctx.make_bundle()` 取服务束 + 调用 services + 结果转换；参数校验由基于 jsonschema 的契约中间件强制。

## 关键文件

| 文件 | 职责 |
|---|---|
| `server.py` | `build_mcp_server`：组装服务、注册工具、挂契约中间件 |
| `context.py` | `McpContext`：服务束、鉴权/scope/租户守卫、配额闸门、工具包装 |
| `contract.py` | `ArgumentContract` 中间件：未知参数、schema 校验、类型宽容化、错误渲染、结果压缩 |
| `tools/retrieval.py` | search / answer / read_doc / catalog 等检索工具 |
| `tools/discovery.py` | doc_probe / repo_probe / resolve / 搜索发现工具 |
| `tools/mining.py` | 六类 insight 与 mining_health 工具 |
| `tools/ops.py` | review / job_show / submit_feedback |
| `tools/index_requests.py` | 索引请求的提交/查询/等待/审批 |

## 功能清单

### 工具注册与可见性（server.py + context.py）

- 组合根装配：`build_gateway`/`build_vector`/`build_search_service`/`build_discovery_service`，再直接构造 `PageService`/`LibraryService`/`MiningReadService`/`ReviewService` 装入 `McpContext`。
- 可见工具集 = 白名单（缺省全部）− 黑名单，未知工具名直接抛 `ConfigError`（fail fast）。
- 27 个工具分五组注册（7 检索 + 5 发现 + 7 挖掘 + 3 运维 + 6 索引请求），仅 enabled 集合内的被 `mcp.tool()` 注册。
- **`wrap` 守卫**：22 个只读工具免写权限；answer/web_search/github_search/resolve 四个经配额闸门；缺凭据报 `unauthorized`，缺 scope 写审计 `auth.forbidden` 并抛 `forbidden`；`with_next` 附加"下一个建议工具"引导。

### 参数契约中间件（contract.py）

非 pydantic，而是 jsonschema `Draft202012Validator` + `best_match` 的 `ServerMiddleware`：

- 拦截 `tools/call`：先查未知参数（报错给出 72 字符内的可接受参数清单），再全量 schema 校验，违规翻译成一句话并指名字段。
- `_coerce` 类型宽容化：接受 SDK 常见的字符串拼写（"1"→int、"true"→bool、字符串化 JSON 数组/对象），但绝不把布尔当整数。
- `ToolFailure(LibrefError, ToolError)` 让 MCP runner 原样保留错误 payload；失败结果剥掉 runner 的 "Error executing tool" 前缀只留紧凑 payload。
- `_compact` 把 `structuredContent` 以无缩进紧凑 JSON 重写进 content，省约四分之一 payload 体积。

### 工具清单（按组）

- **retrieval**：`search`（limit/preset/block_type/exclude_deprecated/aligned_only；`suggest=True` 且无结果时追加发现建议，失败不丢弃已有结果）、`answer`（缺 chat 配置报 `unavailable` + hint）、`read_doc`（mode=page|window|section，window 必给 sort_order、section 必给 source_group_id）、`library_search`、`web_search`、`github_search`、`catalog`。
- **discovery**：`doc_probe`、`repo_probe`（MCP 侧 `allow_local=False` 只允许远程探查）、`repo_dependencies`、`link_stats`、`resolve`（实时网络调用，target=docs|repo）。
- **mining**：`api_insight`（4 视图）、`pattern_insight`（**11 视图**：patterns/evidence/tree/clusters/health/labels/snippets/facets/feature_summary/cluster_labels/drift）、`doc_insight`（4 视图）、`symbol_insight`、`call_insight`、`import_insight`、`mining_health`；参数校验经 `mining_read.views` 的 `validate_*`/`require_api`。
- **ops**：`review`（list 需 `review:read`，其余 `review:write`，状态冲突映射 `review_conflict`）、`job_show`、`submit_feedback`（需 `feedback:write`）。
- **index_requests**：`request_repo_index`/`request_docs_index`（需 `index:request`，参数覆盖 update/fresh/subdir/only_files/exclude_patterns/max_files/start_url 等）、`index_request_status`、`wait_index_request`（timeout≤900s，轮询≤5s，超时附 `timed_out: true`）、`list_index_requests`、`review_index_request`（需 `index:approve`）。

## 对外接口

`build_mcp_server(conn, settings, context)`；stdio 由 CLI `serve-mcp`/`serve --stdio` 驱动（dev 模式注入固定 principal），HTTP 挂载到 web 应用的 `/mcp` 路径。

## 依赖与约束

只经 `services.factory` 与 services 读模型访问数据；不写 SQL、不碰向量引擎。工具 schema 与 web REST 的查询参数保持一一对应（同一 services 方法），契约中间件保证入参先校验后进入服务层。
