# CLI 客户端

`librefctl` 是独立的 HTTP 客户端，供 skill、脚本与人工调用方使用。它只发 HTTP 请求，不打开数据库、不读取服务端配置、不调用服务层，也不接受本机路径。所有权限由服务端 scope 判定。发行单元位于 `packages/librefctl/`，可独立于服务端安装。

## 连接

地址与凭据按以下顺序取值：命令行参数优先，其次环境变量。

| 项 | 参数 | 环境变量 | 缺省 |
|---|---|---|---|
| 服务端地址 | `--base-url` | `LIBREF_BASE_URL` | `http://127.0.0.1:8001` |
| Bearer Key | `--api-key` | `LIBREF_CLIENT_API_KEY` | 空（dev 模式可放行） |
| 单次请求超时 | `--request-timeout` | `LIBREF_CLIENT_TIMEOUT` | 30 秒 |

`--pretty` 缩进输出。客户端不扫描 `libref.toml`，不从 `web_host/web_port` 推导地址。

## 输出契约

所有结果都是 stdout 上的单行 JSON 对象：

* 成功：`{"ok": true, "data": <服务端响应>, "meta": {...}}`；
* 失败：`{"ok": false, "error": {"code", "message", "field", "request_id"}}`。

服务端错误信封与 FastAPI 422 校验体统一映射到同一错误对象；网络失败映射为 `unavailable`。退出码沿用三档：用法与配置错误 2，运行失败 1，成功（含空结果）0。降级原因与等待状态放在 `data` 或 `meta`，不写 stderr。字段形状以 Web API 为真相源，客户端不复制响应模型。

## 命令

`librefctl --help` 列出全部命令，子命令按域分组。

| 域 | 命令 | 远端映射 |
|---|---|---|
| 连通 | `health`、`me` | `GET /health`、`GET /me` |
| 检索 | `search`、`answer`、`read`、`catalog`、`resolve` | `GET /search`、`/answer`、`/read`、`/catalog`、`/resolve` |
| 发现 | `discover`、`search-web`、`search-github` | `GET /library-search`、`/web-search`、`/github-search` |
| 洞察 | `api-insight`、`pattern-insight`、`doc-insight`、`symbol-insight`、`call-insight`、`import-insight`、`link-stats`、`repo-deps`、`mining-health` | 同名 `GET` 路由 |
| 探查 | `doc-probe`、`repo-probe` | `POST /doc-probe`、`POST /repo-probe`；仓库只接受远端地址 |
| 写入 | `ingest-docs`、`mine`、`induce-labels` | `POST /scrape`、`POST /jobs` |
| 流程 | `request submit/list/show/approve/reject/cancel/wait` | `/index-requests` 系列路由 |
| 任务 | `job list/show/cancel/wait` | `/jobs` 系列路由 |
| 审核 | `review list/approve/conflict/reopen` | `GET/POST /review` |
| 反馈 | `feedback submit/list/resolve` | `/feedback` 系列路由 |

参数名与 Web API 字段一致，重复参数用可重复选项传入（如 `--only-file`、`--pattern`、`--resolve-kind`），JSON 对象用 `--scope`、`--target-filter` 传入。`--dedup/--no-dedup` 对应三态布尔。

`request wait` 与 `job wait` 按服务端状态轮询到终态，等待状态只存在于客户端本次进程；`--timeout` 默认 300 秒，超时返回 `unavailable`。`request submit --target-prefix/--target-name` 会合并为服务端的 `target_filter`。仓库入库必须走索引申请，不能提交本机路径。

## 管理命令

`admin` 命令组面向云部署的运维自动化，独立于 skill 常用命令：

| 命令 | 远端映射 |
|---|---|
| `admin principal list [--tenant-id ID]` | `GET /admin/principals` |
| `admin key list [--tenant-id ID]` | `GET /admin/keys` |
| `admin key issue --name NAME [--role ROLE]... [--scope SCOPE]...` | `POST /admin/keys` |
| `admin key attach PRINCIPAL_ID [--scope SCOPE]...` | `POST /admin/principals/{id}/keys` |
| `admin key revoke KEY_ID` | `DELETE /admin/keys/{id}` |
| `admin key revoke-all PRINCIPAL_ID` | `DELETE /admin/principals/{id}/keys` |

这些命令仍由服务端 `admin:keys` scope 授权，客户端不做本地权限判断。**不要**把带 `admin:keys` 的 key 注入 skill 运行环境；签发使用独立 operator key。签发结果中的明文 key 只出现在成功信封的 `data.key`，客户端不缓存、不在超时后自动重试写入；签发超时必须先 `admin key list` 对账。
