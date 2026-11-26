# cli — 本地命令行出口

## 职责

`libref` 命令：服务器启动与本地维护。只做参数解析 + `services/` 调用 + 结果格式化，无业务逻辑；远程 source（http/https/git+）明确拒绝并提示改用 `librefctl request submit`。

## 关键文件

| 文件 | 职责 |
|---|---|
| `__init__.py` | `build_parser`/`main`：解析后调 `args.func(args)`；异常经 `normalize` 归一为 `LibrefError` 走统一失败出口，未知异常带 traceback 抛出 |
| `parsers.py` | 完整 argparse 命令树 |
| `common.py` | 共享装配：配置加载、日志初始化、sqlite 连接 + 迁移、向量栈组合根装配、错误渲染 |
| `local_commands.py` | 本机直接执行的索引/探查/挖掘命令 |
| `ops_commands.py` | 运维、服务启动、身份管理命令 |
| `key_commands.py` | 主体与 API Key 管理 |

## 功能清单

### 全局参数（common.py）

所有子命令统一携带 `--config`、`--log-level`、`--log-file`、`--log-format`、`--json`。`_load_settings` 合并 CLI 日志覆盖后调 `config.load_settings` 并 `setup_logging`；`_build_vector` 经 `services.factory` 装配向量库与 embed 函数。错误输出：错误码一行 + hint + next_actions + request_id，`--json` 时额外在 stdout 输出一条 JSON。

### 本地命令（local_commands.py）

- `local index-repo <library> <version> <source>`：本机路径直接调 `services.code.code_ingest.index_repo`；支持 `--subdir/--only-file/--exclude-pattern/--max-files/--source-role/--target-prefix/--target-name` 等；拒绝远程 source。
- `local probe-repo`：调 `services.code.probe.probe_repo`，输出 probe 模型 JSON。
- `local mine`：调 `mining_refresh.mine_patterns`，支持 `--minsup/--topn/--maxlen/--minlen/--include-ambiguous/--cluster-threshold/--macro-policy` 等，逐行打印 `support + pattern JSON`。
- `local induce-labels`：调 `mining_refresh.induce_labels`。
- `local doc-features refresh`：调 `feature_refresh.refresh_doc_features`（`--scope` 七选一）。

### 运维命令（ops_commands.py）

- `init-db`（应用迁移）、`smoke-local`（内建 ThreadingHTTPServer 假站点跑端到端冒烟，stored≥1 退出 0）。
- `reembed`/`reconcile`：`MaintenanceService.reembed_library` / `reconcile_version`。
- `fts-rebuild`/`fts-check`：FTS 索引重建与一致性校验（不同步退出 1）。
- `vectors-status`/`vectors-optimize`：向量后端 `diagnostics()` / `optimize(wait=True)`。
- `review {list|approve|conflict|reopen}`、`job-show`/`job-cancel`、`config-show`（`redacted_dict` 脱敏快照 + 配置指纹）。
- `purge`、`deletions`（列表/清理过期删除日志）、`role-audit`（角色冲突扫描，strict 有冲突退出 1）、`role-split`（dry-run 支持，新密钥明文只打印一次）。

### 服务启动（cmd_serve）

- `serve --stdio` 走 stdio MCP；否则组装：FastAPI web 应用 + MCP streamable HTTP app 以 `Mount` 合并为一个 Starlette 应用。
- 中间件顺序：AuthMiddleware（最内）→ CORS → FastAPI AsyncExitStackMiddleware → SpaMiddleware（SPA 回退）。
- lifespan：MCP session manager 启动、JobManager 恢复（`recover(requeue=True)`）、注册 scrape/index-repo/mine-patterns/induce-labels 四类 runner 并启动按 kind 分派的 worker。
- 启动前检查：enforced 模式下幂等创建 bootstrap 管理员、监听地址校验、配置指纹校验（分叉拒绝服务）。
- 配置了 allowed_hosts/allowed_origins 时给 MCP 传 `TransportSecuritySettings` 做 DNS rebinding 防护。

### 身份命令（key_commands.py）

`principal list`、`key list/issue/attach/revoke/revoke-all`，全部对接 `services.infra.auth`；role/scope 枚举来自 `ROLE_SCOPES`/`KNOWN_SCOPES`；每个操作写审计（`cli.keys.issue ... result=denied` 等失败也记）；过期时间 ISO 8601 校验。

## 依赖与约束

依赖 services（唯一业务入口）、db（连接与迁移）、config（加载与指纹）。与 librefctl 的分工：本机路径索引用本 CLI，远程/队列式操作走 librefctl HTTP 客户端。
