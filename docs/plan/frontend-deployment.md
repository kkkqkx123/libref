# 前端部署落地方案

> 范围：`webui/` 构建产物在镜像、compose 与 nginx 中的交付方式。本文只写结论、边界与实施顺序，不贴完整配置。

## 现状

后端已有 `web_spa_enabled/web_spa_dir`、`/assets` 静态挂载与末位 SPA 回退；组合服务与独立 Web 应用都注册了同一中间件。部署侧仍按纯 API 组织：镜像不构建也不拷贝前端，compose 不注入产物目录，nginx 示例仍声明无静态职责。结果是按示例栈启动只能得到 API，`webui/dist` 即使存在也不会进入容器。

## 目标

- 默认单镜像单端口：镜像自带前端产物，Python 服务直接托管外壳、哈希资产与深链回退。
- nginx 保持纯反代；只在需要静态卸载时改用静态示例，且不得在 nginx 里枚举 API 路由。
- 同一份产物服务两种拓扑；深链始终由末位回退兜底，避免动静分流产生路由镜像。
- 配置模板、运维文档与架构结论同步更新，不保留“纯 API”旧叙述。
- Podman 不复用 Docker compose，使用独立脚本入口，设计见 `docs/plan/podman-deployment.md`。

## 默认方案：后端直接托管

镜像增加 Node 构建阶段，`npm ci` 后用 `npm run build` 产出 `webui/dist`，运行时镜像拷入固定目录并设置 `LIBREF_WEB_SPA_DIR` 指向它。compose 沿用单服务栈，数据卷只放数据库、快照与仓库，前端属于镜像层。nginx 继续全量转发，不改路径分流；浏览器同源请求的 POST 也带 Origin，因此 `allowed_origins` 必须精确包含对外站点源。

配置缺失或目录缺 `index.html` 时仍按纯 API 启动并告警，这一行为不改成硬失败，便于无前端的轻量部署。

## 可选方案：nginx 托管静态

先由 CI 构建 `webui/dist` 并部署到 nginx 静态目录；nginx 只对 `/assets/` 与 `/` 做静态响应，其余路径（含深链、API、MCP）仍走后端。后端保留同一份产物与 SPA 回退，深链因此不需要在 nginx 侧维护路由白名单。禁止在 `location /` 使用整站 HTML 回退，否则未知 API 路径会被外壳吞没。

跨源拓扑下，前端构建期写入 `VITE_API_BASE`，后端 `allowed_origins` 指向页面源、`allowed_hosts` 指向 API 对外 Host；同源拓扑两者相同且前端基址留空。

## 实施顺序

1. 清理构建上下文：`.dockerignore` 排除 `webui/node_modules` 与本地 `webui/dist`。
2. `Dockerfile` 增加前端构建阶段，运行时拷贝产物并设置 `LIBREF_WEB_SPA_DIR`。
3. 更新 `deploy/nginx.conf.example` 的职责说明；新增 `deploy/nginx.static.conf.example` 作为静态托管示例，并在 compose 注释中说明镜像自带前端。
4. 同步配置模板：`.env.example`、`libref.example.toml`、`deploy/env.dev.example`、`deploy/env.prod.example`、`deploy/libref.toml.example`，统一同源来源示例。
5. 同步文档：`deploy/README.md`、`docs/ops/deployment.md`、`docs/architecture/frontend-hosting.md`、`docs/app/config.md`、`docs/plan/frontend/README.md`、`webui/README.md`。
6. 验证：前端构建与 SPA 集成测试通过；按两种拓扑检查外壳、资产缓存、深链、API 404 与 MCP 代理。
