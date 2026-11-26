# web — HTTP REST 出口

## 职责

FastAPI REST API 与 SPA 壳的挂载：检索、发现、文档阅读、挖掘洞察、审核、索引请求、作业、配额与身份管理。薄出口层——路由只做参数模型解析 + `services/` 调用 + pydantic 响应封装。

## 关键文件

| 文件 | 职责 |
|---|---|
| `app.py` | `build_web_app`：应用组装、中间件栈、路由注册、异常处理 |
| `api.py` | 全部请求/响应 pydantic 模型（统一信封 + 错误码字面量） |
| `middleware.py` | `AuthMiddleware`：鉴权、CORS、配额闸门、请求上下文与指标 |
| `request_context.py` | `WebRequestContext`：请求级服务束、鉴权守卫、配额门 |
| `errors.py` | 错误码收窄与 `error_body` 统一错误信封 |
| `spa.py` | SPA 回退中间件与静态资源缓存 |
| `routes/` | diagnostics/docs/mining/review/index_requests/discovery/jobs/feedback/admin 九组路由 |

## 功能清单

### 端点总览（约 35 个）

- **diagnostics**：`GET /health`（公开，ok + 配置指纹）、`GET /me`（当前主体）、`GET /metrics`（进程计数器：请求数、配额拒绝、token 用量、延迟）、`GET /readyz`。
- **docs**：`GET /catalog`（列库/列版本）、`GET /search`（preset 三档）、`GET /answer`（检索 + 接地生成）、`GET /read`（page/window/section 三模式）、`GET /pages`、`GET /library-search`。
- **mining**：`GET /api-insight`、`/pattern-insight`、`/doc-insight`、`/symbol-insight`、`/call-insight`、`/import-insight`、`/mining-health`——视图枚举与 MCP 工具一一对应。
- **review**：`GET/POST /review`（list/approve/conflict/reopen）。
- **index_requests**：`POST/GET /index-requests`、`GET /index-requests/pending`、`GET /{id}`、`POST /{id}/cancel|approve|reject`。
- **discovery**：`POST /scrape`（提交文档抓取作业）、`POST /doc-probe`、`POST /repo-probe`、`GET /jobs`、`GET /resolve`、`GET /web-search`、`GET /github-search`、`GET /repo-deps`、`GET /link-stats`。
- **jobs**：`POST /jobs`（kind=mine_patterns|induce_labels）、`POST /jobs/{id}/cancel`。
- **feedback**：`POST/GET /feedback`、`POST /feedback/{id}/resolve`。
- **admin**（需 admin 权限）：principals/keys 的列发吊销、配额策略读写、配置刷新。

### 请求/响应模型（api.py）

全部边界是 pydantic：统一信封 `Envelope[ItemT]`（items/count + degraded/reasons）与 `InsightEnvelope`；检索/答案/阅读/六类洞察的 Query-Response 对；`KnownErrorCode` 把服务错误码收窄到已知字面量表；429 错误附 resource 与 reset_at。

### 中间件（middleware.py）

`AuthMiddleware` 贯穿每个请求：

1. 公开路径（/health、/readyz、/、/assets/*）放行；OPTIONS 直接 200。
2. `authenticate_request`（Bearer + Origin 校验，dev 模式注入固定 Principal）；失败写审计 `auth.denied`。
3. 配额闸门 `quota.enforce(resource="http_requests")`，超限 429（配额簿记故障时 fail open）。
4. 生成 12 位 request_id，写入 observe contextvar（request/route/principal/tenant/status/duration），响应附 `x-request-id` 与 `x-elapsed-ms`，记 `http_requests_total{route,status}` 与延迟指标。

### 请求级服务束（request_context.py）

`req_bundle(request, principal)` 优先经可插拔 context 取最新生效配置下的服务束（每请求开连接，`close_rc` 归还），无 context 时回落构造期单连接束；`_guard`/`_read_guard` 做读写鉴权；`quota_gate` 对提交类端点做 429；`IndexRequestError`/`QuotaDenied` 翻译为带稳定码的 `ApiRejection`。

### SPA（spa.py）

`spa_dir_for` 解析到含 index.html 的目录时挂 `/assets` 静态目录；`SpaMiddleware` 对 GET + `Accept: text/html`、非 API、非 `/mcp` 前缀、响应为 401/404 的请求回退返回 index.html（no-store）；/assets 命中加一年 immutable 缓存。web 包内唯一的"页面"即该 SPA shell。

## 对外接口

`build_web_app(conn, settings, *, vec_store, embed, rerank, chat, context)`；由 CLI `serve` 组装进 Starlette 应用（MCP 以 Mount 并列）。

## 依赖与约束

只经 services 访问数据；pydantic 模型与 MCP 工具 schema 派生自同一 services 方法，无字段漂移。`librefctl`（独立 HTTP 客户端包）按本层路由契约构造请求，契约由 `tests/unit/test_librefctl_*` 验证。
