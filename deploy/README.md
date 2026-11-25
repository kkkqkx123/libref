# 部署示例配置

本目录提供**可直接起用的示例栈**：镜像构建（根 `Dockerfile`）、`compose.yml`（libref + Qdrant）、nginx 反向代理示例，以及鉴权模式切换配置。镜像在 Node 构建阶段产出 `webui/dist`，运行时由 Python 服务直接托管前端，nginx 默认只做 TLS 终止与全量转发；需要 nginx 托管静态资产时改用 `deploy/nginx.static.conf.example`。托管设计见 `docs/architecture/frontend-hosting.md`，实施顺序见 `docs/plan/frontend-deployment.md`。

服务端口以 `[web]` 的 `web_port`（默认 `8001`）为唯一真相源，`Dockerfile`、`compose.yml`、`nginx.conf.example` 中的端口都与之对齐；改端口时三处随配置一起改。

Podman 使用独立入口 `deploy/podman/deploy.sh`，本目录的 Docker 配置不做 Podman 适配；用法与边界见 `deploy/podman/README.md`。

| 文件 | 用途 |
|---|---|
| `env.dev.example` | dev 模式：无鉴权、只绑回环 |
| `env.prod.example` | enforced 模式：每请求需 Bearer API Key |
| `libref.toml.example` | 非密钥配置文件示例（`[auth]` / `[server]` / `[mcp]` / `[web]` / `[logging]`） |
| `compose.yml` | 示例编排：libref + Qdrant，数据走命名卷，libref 只绑回环 |
| `nginx.conf.example` | 反向代理示例：TLS 终止、请求体上限、MCP 长连接头 |
| `nginx.static.conf.example` | 可选静态示例：nginx 发 `/assets/` 与 `/`，其余路径代理到后端 |
| 本文件 | 切换步骤与约束 |

## 命名约束

根 `.gitignore` 的规则是 `.env` / `.env.*` / `!.env.example`，带点前缀的 `deploy/.env.*.example` 会被忽略、进不了版本库。因此示例文件一律命名为 `env.*.example`（无前导点）。使用时：

```bash
cp deploy/env.dev.example .env          # 或 env.prod.example
cp deploy/libref.toml.example libref.toml
```

`.env` 与 `libref.toml` 都不进版本库。`libref.toml` 的完整字段模板见仓库根 `libref.example.toml`，本目录的 `libref.toml.example` 只节选部署常用项。

## dev 模式

1. 复制 `env.dev.example` 为 `.env`，设置 `LIBREF_AUTH_MODE=dev`。
2. 保持 `LIBREF_WEB_HOST=127.0.0.1`。服务启动时会校验：dev 模式 + 非回环监听直接拒绝启动，避免无鉴权服务暴露到网络。
3. 中间件注入固定 `Principal`（`LIBREF_DEV_PRINCIPAL`），不校验 API Key；`librefctl` 调用无需 `LIBREF_CLIENT_API_KEY`。
4. 仅供本地开发，**不得用于生产**。

## enforced 模式

1. 复制 `env.prod.example` 为 `.env`，设置 `LIBREF_AUTH_MODE=enforced`。
2. 首次启动前用 `openssl rand -hex 32` 生成 `LIBREF_BOOTSTRAP_ADMIN_KEY`；服务启动时幂等创建首个平台管理员主体与其 API Key，解决「空库无管理员」的死锁。之后凭据签发/吊销全部走 `POST /admin/keys`。
3. 把签发给 `librefctl` 或外部 AI 助手的 Key 填入调用方环境的 `LIBREF_CLIENT_API_KEY`，`LIBREF_BASE_URL` 指向服务地址；`librefctl` 每个请求带 `Authorization: Bearer <key>`。
4. 非回环部署必须设置 `LIBREF_ALLOWED_HOSTS` / `LIBREF_ALLOWED_ORIGINS`。前者供 MCP 传输层做 DNS rebinding 保护；后者同时用于鉴权来源精确匹配与 CORS，禁止通配符和带路径的来源。浏览器客户端还需放行 `Mcp-*` 请求头并暴露 `Mcp-Session-Id`。
5. 外部流量经反向代理终止 TLS，服务本身只绑回环或私有网络。

## 主体与 scope

| 角色 | 主体类型 | 主要 scope |
|---|---|---|
| 开发者 | human | `read`、`feedback:write` |
| 知识库管理员 | human | `read`、`review:read/write`、`feedback:read/resolve` |
| 系统管理员 | human | `read`、`ingest:write`、`jobs:write`、`admin:keys`、`admin:config` |
| 平台管理员 | human（首次引导） | 全部 scope，含 `platform:admin` 跨租户权限 |
| 外部 AI 助手 | service（机器主体，无人工登录） | `read`；可按需追加 `feedback:write` |

签发 Key 时 scope 必须是调用者已有 scope 的子集，主体租户必须属于调用者可管理范围；只有平台管理员能创建 `*` 租户主体或跨租户签发。凭据即身份，无密码、无会话。

## 密钥边界

- **请求侧凭据**（用户/助手 API Key、引导 Key）与**上游密钥**（`LIBREF_EMBEDDING_API_KEY` / `LIBREF_CHAT_API_KEY` / `LIBREF_DISCOVERY_GITHUB_TOKEN`）只走环境变量，写入 `libref.toml` 会在加载时被拒绝（`src/libref/config.py:490-494`）。
- 修改上游密钥后调用 `POST /admin/config/refresh` 重建冻结快照；不做热更新。
- `config-show` 打印脱敏快照，`/health` 只回配置指纹，均不泄露密钥。

## 前端产物

- 默认：`docker compose -f deploy/compose.yml up --build` 会在镜像内执行 `npm ci` 与 `npm run build`，把 `webui/dist` 拷入运行时镜像；`LIBREF_WEB_SPA_DIR` 由镜像设为 `/app/webui/dist`，无需额外卷。源码直接运行时未构建前端则保持纯 API 并告警。
- nginx 静态模式：先在 `webui/` 执行 `npm ci && npm run build`，把 `dist/` 部署到 nginx 静态目录并套用 `nginx.static.conf.example`；后端继续持有同一份产物以提供深链回退。两种模式的前端版本必须一致。
- 同源部署把 `LIBREF_ALLOWED_ORIGINS` 与 `LIBREF_BASE_URL` 都设为对外站点源；跨源部署让前端在构建期写入 `VITE_API_BASE`，并把 `allowed_origins` 设为页面源、`allowed_hosts` 设为 API 对外 Host。

## 示例栈启动

1. 在 `deploy/` 下复制 `env.prod.example` 为 `libref.env` 并填入真实密钥（compose 的 `env_file` 指向该文件；根 `.env` 只供本地直接运行时使用）。
2. 在仓库根执行 `docker compose -f deploy/compose.yml up --build -d`，健康检查轮询 `http://127.0.0.1:8001/health`。默认向量后端为本地 simvec，不需要 Qdrant；仅当设置 `LIBREF_VECTOR_BACKEND=qdrant` 时，加 `--profile qdrant` 同步启动 Qdrant 服务。
3. 对公暴露时在宿主机套用 `nginx.conf.example`（改好域名与证书路径），服务本身继续只绑回环。

## 启动命令

单一组合 `libref serve` 同进程提供 Web 路由与 `/mcp`（Web 在前、`Mount("/")` 在后，顶层 `lifespan` 持有 `session_manager`）；`--stdio` 切到标准输入输出供本地客户端。`serve-mcp` 只保留标准输入输出，流式 HTTP 统一由 `serve` 提供。详见 `docs/app/cli.md`。
