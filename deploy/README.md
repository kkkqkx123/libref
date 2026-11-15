# 部署示例配置（本阶段）

本目录只提供**示例配置**，用于切换鉴权模式与验证 4 角色设计；镜像、compose、TLS 终止与 CI 属部署设计，本阶段不做（见 `docs/plan/multi-user-production-deferred.md` 的 T3/T4）。

| 文件 | 用途 |
|---|---|
| `env.dev.example` | dev 模式：无鉴权、只绑回环 |
| `env.prod.example` | enforced 模式：每请求需 Bearer API Key |
| `libref.toml.example` | 非密钥配置文件示例（`[auth]` / `[client]` / `[server]` / `[mcp]` / `[web]` / `[logging]`） |
| 本文件 | 切换步骤与约束 |

## 命名约束

根 `.gitignore:25-27` 的规则是 `.env` / `.env.*` / `!.env.example`，带点前缀的 `deploy/.env.*.example` 会被忽略、进不了版本库。因此示例文件一律命名为 `env.*.example`（无前导点）。使用时：

```bash
cp deploy/env.dev.example .env          # 或 env.prod.example
cp deploy/libref.toml.example libref.toml
```

`.env` 与 `libref.toml` 都不进版本库（`.gitignore:25-30`）。

## dev 模式

1. 复制 `env.dev.example` 为 `.env`，设置 `LIBREF_AUTH_MODE=dev`。
2. 保持 `LIBREF_WEB_HOST=127.0.0.1`。服务启动时会校验：dev 模式 + 非回环监听直接拒绝启动，避免无鉴权服务暴露到网络。
3. 中间件注入固定 `Principal`（`LIBREF_DEV_PRINCIPAL`），不校验 API Key；CLI remote 调用也无需 `LIBREF_CLIENT_API_KEY`。
4. 仅供本地开发，**不得用于生产**。

## enforced 模式

1. 复制 `env.prod.example` 为 `.env`，设置 `LIBREF_AUTH_MODE=enforced`。
2. 首次启动前用 `openssl rand -hex 32` 生成 `LIBREF_BOOTSTRAP_ADMIN_KEY`；服务启动时幂等创建首个系统管理员主体与其 API Key，解决「空库无管理员」的死锁。之后凭据签发/吊销全部走 `POST /admin/keys`。
3. 把签发给 CLI 或外部 AI 助手的 Key 填入 `LIBREF_CLIENT_API_KEY`，`LIBREF_BASE_URL` 指向服务地址；CLI 用 `--remote` 时每个请求带 `Authorization: Bearer <key>`。
4. 非回环部署必须设置 `LIBREF_ALLOWED_HOSTS` / `LIBREF_ALLOWED_ORIGINS`，它们传给 MCP 2.3.0 的 `TransportSecuritySettings` 做 DNS rebinding 保护；浏览器客户端还需放行 `Mcp-*` 请求头并暴露 `Mcp-Session-Id`。
5. 外部流量经反向代理终止 TLS，服务本身只绑回环或私有网络。

## 四类主体与 scope

| 角色 | 主体类型 | 主要 scope |
|---|---|---|
| 开发者 | human | `read`、`feedback:write` |
| 知识库管理员 | human | `read`、`review:read/write`、`feedback:read/resolve` |
| 系统管理员 | human（平台级） | `read`、`ingest:write`、`jobs:write`、`admin:keys`、`admin:config` |
| 外部 AI 助手 | service（机器主体，无人工登录） | `read`；可按需追加 `feedback:write` |

完整的角色到端点/工具映射见 `docs/plan/multi-user-production-deployment.md` §5.2。外部 AI 助手由系统管理员签发 Key，凭据即身份，无密码、无会话。

## 密钥边界

- **请求侧凭据**（用户/助手 API Key、引导 Key）与**上游密钥**（`LIBREF_EMBEDDING_API_KEY` / `LIBREF_CHAT_API_KEY` / `LIBREF_DISCOVERY_GITHUB_TOKEN`）只走环境变量，写入 `libref.toml` 会在加载时被拒绝（`src/libref/config.py:490-494`）。
- 修改上游密钥后调用 `POST /admin/config/refresh` 重建冻结快照；不做热更新。
- `config-show` 打印脱敏快照，`/health` 只回配置指纹，均不泄露密钥。

## 启动命令

单一组合 `libref serve` 同进程提供 Web 路由与 `/mcp`（Web 在前、`Mount("/")` 在后，顶层 `lifespan` 持有 `session_manager`）；`--stdio` 切到标准输入输出供本地客户端。旧 `serve-mcp/serve-web` 保留兼容，其中 `serve-mcp --http` 使用 `[mcp]` 的监听配置，组合 `serve` 使用 `[web]` 的监听配置。详见 `docs/app/cli.md`。
