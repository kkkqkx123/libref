# 前端功能规划总览

> 范围：`webui/` 立项后的前端实现清单，以 `src/libref/web/` 现有 REST 能力为唯一需求来源。
> 技术栈与托管结论分别见 `docs/architecture/tech-stack.md`（Vue 3 + Element Plus，
> `webui/` 独立 npm 工程）与 `docs/architecture/frontend-hosting.md`（Python 自托管、
> SPA 回退挂末位、开发期经 `VITE_API_BASE` 直连 `8001`）。
> 本目录只描述做什么与依赖哪个后端端点，不规定组件实现细节。

## 后端现状摘要

Web 出口提供约三十个根路径端点与挂载于 `/mcp` 的 MCP 应用，并托管自带的前端产物。
路由层只做参数解析与 `services/` 调用转发，业务逻辑全部在服务层，因此前端与 CLI/MCP
看到的数据口径天然一致。FastAPI 自带 OpenAPI 描述，前端以其为接口契约来源。

## 功能域划分

| 功能域 | 对应页面 | 后端端点组 |
|---|---|---|
| 检索问答 | 搜索页、问答页、文档阅读器 | `/search`、`/answer`、`/read`、`/pages`、`/catalog` |
| 源码洞察 | API 洞察页、调用页、符号页、导入页、模式页 | `/api-insight`、`/call-insight`、`/symbol-insight`、`/import-insight`、`/pattern-insight`、`/mining-health`、`/repo-deps` |
| 入库引导 | 入库向导页 | `/resolve`、`/library-search`、`/web-search`、`/github-search`、`/doc-probe`、`/repo-probe`、`/scrape` |
| 任务运维 | 任务中心页 | `/jobs`、`/jobs/{id}`、`/jobs` 新建、`/jobs/{id}/cancel`、`/scrape` |
| 审核反馈 | 审核页、反馈页 | `/review`（GET/POST）、`/feedback`（GET/POST）、`/feedback/{id}/resolve` |
| 系统管理 | 管理页、状态条 | `/health`、`/readyz`、`/metrics`、`/admin/keys`、`/admin/config/refresh` |

`/link-stats` 后端能力保留，前端列为后续项，见 `api-mapping.md`。
`/repo-deps` 的实际入口在文库目录页的版本行，不在入库向导。

## 本目录文件

- `pages.md`：页面级功能清单，每个页面的目标、核心操作、空态与降级。
- `api-mapping.md`：端点到前端需求的映射，含参数、权限 scope、错误码处理。
- `components-state.md`：跨页面共享组件、筛选器状态模型、轮询与缓存策略。
- `ops-admin.md`：任务、审核、反馈、管理面的操作流程与非功能要求。
- `phases.md`：分阶段实施方案，阶段划分与每阶段验收标准。
- `style-guide.md`：样式设计与统一规范，令牌单源与共用组件清单。
- `i18n.md`：前端国际化设计方案，词条、语言切换、检测与迁移计划。
- `remaining-tasks.md`：相对本目录的剩余任务、分阶段改法、落地结果，O1–O5 的设计决策与错误码契约（§11），
  以及第二轮复核出的 R1–R7 与决策（§12）。
- `../frontend-schema-typing.md`：API 类型契约收紧的任务清单与分阶段落地记录，
  覆盖响应模型、请求包装器、枚举、错误契约与结构化行数据。
- `../frontend-wire-contract.md`：任务种类、动态错误码、时间表示与多值参数的独立收口方案。

## 实施状态

- 五个阶段已全部实现，见 `webui/`（Vue 3 + Element Plus 单页应用）。
- 后端前置：组合服务的拍平路由已补 FastAPI 路由不变量与 SPA 中间件，
  按请求会话隔离修复了上下文连接复用，见 `tests/integration/test_combined_serve.py`。
- 前端校验：`npm run check`（类型、词条门禁、令牌）与 `npm run build`；
  后端校验：全量 pytest 加 ruff 加 mypy。
- 部署落地：镜像内置前端产物，`docs/plan/frontend-deployment.md` 记录后端自托管与 nginx 静态两种拓扑的实施方式。
- 落地顺序建议：检索问答先行（只读），其次源码洞察（只读），再入库引导与任务中心，
  最后审核反馈与系统管理（写操作多、权限要求高）。
