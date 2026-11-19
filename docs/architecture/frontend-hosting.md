# 前端托管设计

> 定位：前端（`webui/`，见 `tech-stack.md` 与 `module-layout.md` 预留位置）立项后，构建产物由谁托管、如何与现有服务共存。本设计只写结论与边界，不贴完整代码。

## 现状与结论

服务已有约三十个根路径端点与挂载在 `/mcp` 的 MCP 应用，同时具备 SPA 外壳与 `/assets` 静态能力。前端落地后默认由 Python 自托管：镜像内置构建产物，nginx 保持纯反代，不做动静路径拆分。只有在需要静态卸载时才改用 `deploy/nginx.static.conf.example`，且深链仍由后端末位回退兜底。

## 为什么默认 Python 自托管

单镜像单端口与现有 `libref serve` 组合形态天然契合：dev、单机、内网开箱即用，不新增组件，不新增构建链路。uvicorn 发静态大文件的性能弱于 nginx，但管理后台类页面的体量下这不是瓶颈；真到瓶颈那天，缓存（见下）比拆分更先见效。

反之若把静态拆给 nginx 托管，会与"API 占据根路径"的现状正面冲突：nginx 要按前缀把 API 与静态分流，就得在 conf 里逐条枚举 API 前缀，与 `web/app.py` 的路由表形成易漂移的镜像，属重复 fallback 式的脆弱设计，不予采用。

## 路由与挂载顺序

API 端点占据根路径且数量多，MCP 以 `Mount("/")` 挂在最后，因此 SPA 回退必须排在整条链的最末：API 路由先匹配，`/mcp` 次之，其余非常见 API 前缀的 GET 请求才回退到 `index.html`。静态产物走长缓存、`index.html` 走不缓存，这是唯一需要的缓存区分。

## 鉴权与计量边界

外壳与构建产物公开：中间件的公开路径需扩展到精确根路径与产物目录，壳的加载不消耗凭据。数据接口保持现有语义不变：dev 注入固定主体，enforced 逐请求验 Bearer；浏览器 enforced 场景仍需放行 `Mcp-*` 请求头并暴露 `Mcp-Session-Id`（`deploy/README.md` 已有约束）。静态命中不计入按主体的请求配额，只对 API 与 MCP 计数，避免刷新一次页面就烧掉配额。

## 配置

新增两个 `[web]` 键，优先级沿用统一约定（显式参数高于环境变量，高于配置文件，高于内置默认）：总开关默认开，产物目录默认空。镜像通过 `LIBREF_WEB_SPA_DIR` 指向内置产物，源码运行未构建前端时保持纯 API。目录不存在或缺 `index.html` 时只告警并继续以纯 API 形态运行，绝不因此拒绝启动：同一个镜像在前端构建前后都能跑，这是防误用的关键。

## 构建与镜像

`webui/` 保持独立 npm 工程，不进 `pyproject`。`Dockerfile` 用 Node 构建阶段执行 `npm ci` 与 `npm run build`，运行时镜像只拷入 `webui/dist` 并设置 `LIBREF_WEB_SPA_DIR` 指向它；`.dockerignore` 排除本地 `webui/dist` 与 `webui/node_modules`，产物只来自构建阶段。开发期前端 dev 服务器通过 `VITE_API_BASE` 直连本地 `8001`（`.env.development` 指定，服务端 CORS 已放开），不经过 Python 托管链路，也不额外配置代理。

## nginx 叠加（生产可选）

默认 `deploy/nginx.conf.example` 全量转发到 `8001`，不对资产做路径拆分，也不缓存外壳。需要静态卸载时改用 `deploy/nginx.static.conf.example`：nginx 只对 `/assets/` 与 `/` 发静态响应，其余路径继续代理；后端仍持有同一份产物以提供深链回退。跨源部署时前端构建期写入 `VITE_API_BASE`，后端 `allowed_origins` 指向页面源、`allowed_hosts` 指向 API 对外 Host；同源部署两者取同一站点源，前端基址留空。

## 落地步骤（已实现）

配置项（`web_spa_enabled/web_spa_dir`）、末位 SPA 回退（文本导航回退 `index.html`、
API 与 `/mcp` 前缀不受回退吞没）及公开路径扩展（根路径与产物目录）已实现，
组合服务同时补齐了拍平路由绕过的 FastAPI 路由不变量；`webui/` 工程已全阶段实现。
部署侧已补齐镜像内前端构建、`LIBREF_WEB_SPA_DIR` 注入、nginx 反代与静态示例、
配置模板与运维文档，详见 `docs/plan/frontend-deployment.md`。

## 与既有文档的对应

端口以 `web_port`（默认 `8001`）为准，容器与代理示例已对齐；`deploy/README.md` 描述示例栈现状，`docs/plan/frontend-deployment.md` 记录部署实施顺序；`tech-stack.md` 与 `module-layout.md` 的 `webui/` 预留位置是前端工程的落点；`multi-user-production-deferred.md` 的 T9（前端不在本阶段）在本设计落地时标注已启动。
