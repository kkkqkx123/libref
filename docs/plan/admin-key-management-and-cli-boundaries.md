# 密钥管理与 CLI 边界收尾

本文接在 `cli-client-split-followup.md` 之后，收口三项决策：文档探查只保留 HTTP 出口、服务端本机 CLI 补齐密钥管理、`librefctl` 是否开放 admin 密钥管理。其余架构风险一并列为任务。

## 决策一：文档探查统一走 `librefctl doc-probe`

服务端本机 CLI 删除 `probe-docs`。文档探查不读取本机文件系统，没有必须留在服务端的理由；保留它只会形成与 HTTP 探查重复的双路径。

实施范围：

- 从 `src/libref/cli/local_commands.py` 删除 `cmd_probe_docs`，从解析器删除 `probe-docs` 子命令。
- 更新 `docs/app/cli.md`、`docs/features/probe-and-scope.md`、`docs/architecture/mcp-tools.md` 与服务端 CLI 解析测试，不再列出服务端文档探查入口。
- `tests/e2e/probe-discovery/fetch_probe_discovery.py` 已通过 `librefctl` 采集文档探查，无需改动调用路径；只需确认快照与说明不残留旧命令。
- 同步修正 `cli-client-split-followup.md` 中“恢复文档探查”的描述。

仓库探查 `libref local probe-repo` 保留，因为它覆盖本机路径，是方案明确要求留在服务端的能力。

## 决策二：服务端本机 CLI 补齐主体与密钥管理

原方案把主体与密钥管理列为本机运维能力，当前只有角色巡检/拆分，缺少直接管理入口。服务端主机本身已是最高信任边界，因此本机 CLI 可以使用平台级系统调用者，不必模拟 HTTP 身份；但所有操作必须落审计。

命令设计：

| 命令 | 作用 |
|---|---|
| `libref principal list [--tenant TENANT] [--json]` | 列出可见主体、角色、scope 与冲突标记 |
| `libref key list [--tenant TENANT] [--json]` | 列出 key 前缀、主体、scope、有效期、撤销与最后使用时间 |
| `libref key issue --name NAME [--kind KIND] [--tenant TENANT] [--role ROLE]... [--scope SCOPE]... [--expires-at ISO]` | 创建主体并签发 key；明文只回显一次 |
| `libref key revoke KEY_ID` | 撤销单把 key |
| `libref key revoke-all PRINCIPAL_ID` | 撤销某主体全部有效 key |

实现要点：

- 在 `services/infra/auth.py` 暴露一个公开的系统调用者构造函数，复用现有平台角色与 scope，不让 CLI 直接引用私有函数。
- 复用 `create_principal`、`issue_api_key`、`revoke_api_key`、`revoke_principal_keys`、`list_principals` 与现有校验，不复制权限规则。
- `--role`、`--scope` 的取值从 `ROLE_SCOPES`、`KNOWN_SCOPES` 派生，避免命令行枚举漂移。
- 本机操作以 `principal_id=None` 写入 `audit_log`，动作使用 `cli.keys.*` / `cli.principals.*` 前缀，与 Web 路由审计区分。
- 明文 key 只写 stdout，不写日志、不落文件；`--json` 模式下同样只回显一次。
- 角色变更继续走 `role-split` 或 Web 管理路由，不在本机 CLI 重复实现。

测试口径：临时库中验证签发后只存哈希、`key list` 展示前缀、撤销与批量撤销计数、角色互斥和 scope 收窄仍被拒绝、审计行存在且主体为空。

## 决策三：`librefctl` 有条件开放 admin 密钥管理

### 分析

Web API 已经暴露 `/admin/keys`、`/admin/principals` 等路由，权限完全由服务端 `admin:keys` scope 决定。单纯不在客户端做命令，并不会减少权限面，只会让云部署方改用 `curl` 或自写脚本，反而失去统一的错误信封、超时与退出码契约。

另一方面，skill 运行环境通常只应持有 `read` / `feedback:write` 一类 scope。把密钥管理混入检索命令面，会诱导把管理员 key 注入 skill 凭据。

### 结论

**有条件开放**：在 `librefctl` 增加独立的 `admin` 命令组，不进入 skill 常用命令面，也不做兼容别名。

- `librefctl admin key list`
- `librefctl admin key issue`
- `librefctl admin key revoke KEY_ID`
- `librefctl admin key revoke-all PRINCIPAL_ID`
- `librefctl admin principal list`

约束：

- 服务端仍以 `admin:keys` scope 为唯一授权来源；客户端不做本地权限判断。
- 管理凭据必须是独立的 operator key，文档明确禁止把带 `admin:keys` 的 key 注入 skill 运行环境。
- `key issue` 明文只出现在成功信封的 `data.key` 中，客户端不缓存、不重试写入。POST 超时视为结果不明，必须先 `key list` 对账，避免重复签发。
- 不提供 bootstrap admin key 或主体删除命令，保持最小权限面。

### Web 契约缺口

当前 `POST /admin/keys` 把“创建主体”和“签发 key”绑定在一起，且没有对既有主体追加 key 的路由。批量轮换场景下这会导致每次签发都产生一个新主体。

补充 `POST /admin/principals/{principal_id}/keys`，复用 `issue_api_key`，请求体只含 `scopes` 与 `expires_at`，仍要求 `admin:keys`。客户端 `admin key issue` 默认走既有 `POST /admin/keys`；需要为既有主体追加 key 时用 `admin key attach`，对应新路由。两条路径都只回显明文一次。

## 其他架构任务

1. **契约复制漂移**：`librefctl/commands.py` 的路径/参数/枚举、`transport.py:USAGE_CODES`、`app.py:_TERMINAL`、`commands.py:_SOURCE_ROLES` 都与服务端定义重复。增加基于 OpenAPI 的参数级一致性测试和跨包常量对照测试；长期目标是让命令表从 Web schema 生成，而不是手工维护第二份真相。
2. **命令命名冲突**：原 `libref mine` 与 `librefctl mine` 名字相同但语义不同（本机直接执行 vs 提交 job）。服务端本机入口统一收进 `libref local` 子命令组，例如 `libref local mine`、`libref local index-repo`。同步更新文档、测试与探查采集脚本；不做旧名别名。
3. **进程入口清理**：`src/libref/__init__.py` 顶层导入 `libref.cli.main`，lazy export 注释与实际行为不符；pyproject 入口已指向 `libref.cli:main`，删除多余导出或改成真正的惰性实现。
4. **快照说明同步**：`tests/e2e/probe-discovery/outputs` 的 HTTP 用例输出已归一为客户端信封；后续若改命令名，必须同步 meta 的 argv 标签，不再保留旧入口标签。

## 阶段划分

1. 删除服务端 `probe-docs`，同步文档与测试。
2. 服务端本机 CLI 增加 `principal list` 与 `key list/issue/revoke/revoke-all`，补审计与测试。
3. Web 增加对既有主体追加 key 的路由，并以 `admin:keys` 保护。
4. `librefctl` 增加 `admin` 命令组，覆盖既有管理路由与新路由，补传输与差分测试。
5. 服务端本机入口改为 `libref local` 子命令组，清理 `src/libref/__init__.py`。
6. 增加 OpenAPI/常量一致性门禁，更新 CLI/MCP/Web/部署文档与验证命令说明。

## 验证口径

- 全量 `pytest`、`ruff check`、`ruff format --check`、`mypy` 覆盖 `src`、`tests` 与 `packages/librefctl/src`。
- 本机密钥管理集成测试覆盖哈希存储、明文回显一次、撤销计数、权限收窄与审计。
- 客户端 admin 命令用 MockTransport 覆盖成功、422、服务端错误信封、网络失败与签发超时不重试。
- 命令表与 Web 路由的一致性测试升级为参数级字段对照。
- 不执行真实网络 e2e，不使用兼容别名，不新增发行包或版本。
