# Web 权限管理与角色操作矩阵

## 适用范围

本文描述当前服务端实际执行的认证、授权、租户隔离与配额边界，覆盖组合 `serve` 提供的 Web API 与 `/mcp`，以及本机 CLI/stdio 的可信入口。外部身份系统、密码会话与 MCP OAuth 不在当前实现范围内。

## 1. 认证与运行配置

| 配置项 | 配置节 / 环境变量 | 默认值 | 含义 |
|---|---|---|---|
| 鉴权模式 | `[auth] auth_mode` / `LIBREF_AUTH_MODE` | `enforced` | `enforced` 下每个非公开请求必须带 Bearer API Key；`dev` 必须显式开启且只许回环监听 |
| 开发主体名 | `[auth] dev_principal` / `LIBREF_DEV_PRINCIPAL` | `dev-operator` | `dev` 模式下注入的固定平台主体，不校验 Key |
| 引导 Key | `[auth] bootstrap_admin_key` / `LIBREF_BOOTSTRAP_ADMIN_KEY` | 空 | 仅环境变量；首次启动创建平台管理员，之后按已有主体校验 |
| 客户端 Key | `[client] client_api_key` / `LIBREF_CLIENT_API_KEY` | 空 | CLI `--remote` 的 Bearer 凭据，仅环境变量 |
| Key 默认有效期 | `[auth] api_key_default_ttl_hours` | 168 小时 | 签发时不指定过期时间则套用 |
| Key 最大有效期 | `[auth] api_key_max_ttl_hours` | 8760 小时 | 过期时间不得超过该上限；默认有效期也不得超过上限 |
| 允许来源 | `[auth] allowed_origins` / `LIBREF_ALLOWED_ORIGINS` | 空 | 浏览器 Origin 精确匹配；禁止 `*`、`path`、`query`，无 Origin 的非浏览器请求放行 |
| 允许主机 | `[auth] allowed_hosts` / `LIBREF_ALLOWED_HOSTS` | 空 | MCP 传输安全 Host 白名单；未配置时回环监听用回环默认值，非回环监听用监听地址加通配端口；多域名反代应显式配置 |
| MCP 工具可见性 | `[mcp] mcp_enabled_tools` / `mcp_disabled_tools` | 全部可见 | 允许名单减去拒绝名单；未知名启动即报错。此项只控制可见性，不替代 scope 判定 |
| `quota` 开关与阈值 | `[quota] quota_enabled` 等 | 关闭、阈值 0 | 0 表示不限额；数据库策略优先，配置值仅作兜底 |

凭据只经环境变量注入，写进 `libref.toml` 会在配置加载时被拒绝。Web 默认只绑 `127.0.0.1:8001`；对外暴露必须显式改监听并使用 `enforced`。

## 2. 主体与凭据模型

- 主体 `Principal` 携带 `tenant_id`、`kind`、`name`、`roles`、`scopes`、`status`。角色静态展开为 scope，多角色取并集。
- Key 只存 SHA-256 哈希与前缀，明文仅在签发响应中出现一次。吊销、过期、主体非 active 都会使查找失败。
- Key 上的 `scopes` 是**收窄边界**：非空时取「角色 scope ∩ Key scope」；留空表示继承角色全部 scope。签发时要求请求 scope 已知、不超过签发者自己持有的 scope，且不超过目标主体的角色 scope。
- 所有 HTTP 请求走同一个认证入口：Bearer 解析 → Origin 校验 → Key 查找 → 主体解析。公开路径仅 `/health`、`/readyz`、`/` 与 `/assets/*`。
- Web 入口和组合服务的 `/mcp` 共用认证中间件；scope 判定也共用 `services/infra/auth.py` 的原语。

## 3. 角色与 scope

| 角色 | scope | 定位 |
|---|---|---|
| `external` | `read`, `index:request` | 只读外部读者，可申请索引 |
| `developer` | `read`, `feedback:write`, `feedback:read:own`, `index:request` | 可检索、提交反馈并查看本人反馈、申请索引 |
| `kb_admin` | `read`, `review:read`, `review:write`, `feedback:read`, `feedback:resolve`, `index:request`, `index:approve` | 知识库审核、反馈处置与索引审批 |
| `sys_admin` | `read`, `ingest:write`, `jobs:write`, `admin:keys`, `admin:config`, `index:request`, `index:approve` | 本租户入库、任务、凭据与索引审批 |
| `platform_admin` | 上述全部 + `platform:admin` | 平台管理员，跨租户管理与全局可见 |

`platform:admin` 是跨租户提升开关：持有它即可管理任意租户的主体、全局查看数据、处置任意租户的审核与反馈。普通管理员只能管理自身租户，且不能签发高于自身角色或 scope 的凭据。

### 职责分离

`developer`、`kb_admin`、`sys_admin` 三者在同一主体上**至多持一个**：提交、审核、运维构成一条决策链，任意两者合流就成了"自己提交自己审"或"自己配置自己批"。`external` 是只读消费方，与任何角色叠加都不产生闭环，不受约束；`platform_admin` 表达跨租户范围而非租户岗位，豁免该约束（它的 scope 是全集，纳入互斥会让引导主体无法创建）。

违反时 `create_principal` 直接拒绝，`POST /admin/keys` 返回 400，文案给出冲突组合。Key 上的 scope 只能收窄、不能扩权，因此没有"用 key 把角色补回来"的通道。

存量主体可能已持有冲突组合。服务不自动改写（静默削减角色会切断线上凭据），改为启动告警、巡检与显式拆分：

| 命令 | 行为 |
|---|---|
| `libref role-audit` | 逐行列出冲突主体并给出可执行的 `--keep` 选项，退出码恒为 0 |
| `libref role-audit --strict` | 存在冲突时退出码 1，可作 CI 卡口 |
| `libref role-audit --json` | 结构化输出，含冲突组与角色清单 |
| `libref role-split --principal N --keep ROLE --dry-run` | 打印拆分计划，不写库 |
| `libref role-split --principal N --keep ROLE` | 把其余角色装箱到最少量的新主体，各发一把新 key（明文仅一次），原主体只留 `--keep` |

拆分的落点是**主体**而不是"人"：原主体的旧 key 仍然有效，权限按新角色收窄，不会当场断线。浏览器侧可在 `GET /admin/principals` 看到冲突清单，修复仍走上面的 CLI——拆分必须由人指定保留哪个角色，并把新明文交到承接人手里。

约束的边界是"一个主体"，不是"一个人"：同一人持有两个主体即可绕过，完整的职责分离需要外部身份源绑定自然人，当前不在实现范围内。

## 4. 角色操作矩阵

下表假定主体只有该角色且 Key scope 留空。多角色取并集；非空 Key scope 可能进一步减少权限。

| 操作 | 所需 scope | external | developer | kb_admin | sys_admin | platform_admin |
|---|---|---:|---:|---:|---:|---:|
| 检索、目录、页面、洞察、探针、任务查看 | `read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| 查看 `/me` 身份信息 | 已认证 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 提交反馈 | `feedback:write` | — | ✓ | — | — | ✓ |
| 查看反馈 | `feedback:read`（本租户）/ `feedback:read:own`（本人） | — | ✓（仅本人） | ✓ | — | ✓ |
| 处置反馈 | `feedback:resolve` | — | — | ✓ | — | ✓ |
| 审核列表与状态流转 | `review:read` / `review:write` | — | — | ✓ | — | ✓ |
| 提交、查看与取消自己的索引申请 | `index:request` | ✓ | ✓ | ✓ | ✓ | ✓ |
| 审批队列、通过与驳回索引申请 | `index:approve` | — | — | ✓ | ✓ | ✓ |
| 发起抓取入库 | `ingest:write` | — | — | — | ✓ | ✓ |
| 提交 / 取消任务 | `jobs:write` | — | — | — | ✓ | ✓ |
| 列举、签发、吊销 Key | `admin:keys` | — | — | — | ✓ | ✓ |
| 刷新配置快照 | `admin:config` | — | — | — | ✓ | ✓ |
| 跨租户管理、全局可见 | `platform:admin` | — | — | — | — | ✓ |

注意：`kb_admin` 可查看和处置反馈，但**不能提交**反馈；提交反馈属于 `developer` 与平台管理员。`developer` 的反馈可见范围限于本人提交的条目，处理状态对其可见但列表不含他人反馈。`sys_admin` 管理凭据与任务，但不参与审核与反馈处置。索引审批者不能审批自己的申请；持有 `ingest:write` 的主体创建申请时自动入队并写自审批审计，通用 `/jobs` 入口不接受索引类任务。

## 5. Web API 与 scope 对应

| 路由 | 所需 scope |
|---|---|
| `GET /health`、`GET /readyz` | 公开 |
| `GET /`、静态资源 | 公开 |
| `GET /me` | 已认证 |
| `GET /metrics`、`/catalog`、`/search`、`/library-search`、`/answer`、`/read`、`/pages`、`/api-insight`、`/pattern-insight`、`/symbol-insight`、`/call-insight`、`/import-insight`、`/repo-deps`、`/link-stats`、`/mining-health`、`/jobs`、`/jobs/{id}` | `read` |
| `POST /doc-probe`、`/repo-probe`、`GET /resolve`、`/web-search`、`/github-search` | `read` |
| `GET /review` | `review:read` |
| `POST /review` | `review:write` |
| `POST /index-requests`、`GET /index-requests`、`GET /index-requests/{id}`、`POST /index-requests/{id}/cancel` | `index:request`（详情还需 `read`） |
| `GET /index-requests/pending`、`POST /index-requests/{id}/approve`、`/reject` | `index:approve` |
| `POST /scrape` | `ingest:write` |
| `POST /feedback` | `feedback:write` |
| `GET /feedback` | `feedback:read` 看本租户，`feedback:read:own` 只看本人 |
| `POST /feedback/{id}/resolve` | `feedback:resolve` |
| `POST /jobs`、`POST /jobs/{id}/cancel` | `jobs:write` |
| `GET /admin/keys`、`POST /admin/keys`、`DELETE /admin/keys/{id}`、`DELETE /admin/principals/{id}/keys` | `admin:keys` |
| `GET /admin/principals` | `admin:keys`，只读，带角色冲突标记 |
| `GET /admin/quota/policies`、`PUT /admin/quota/policies` | `admin:config` |
| `POST /admin/config/refresh` | `admin:config` |

## 6. MCP 工具与 scope

| 工具组 | 工具 | 所需 scope |
|---|---|---|
| 只读检索与洞察 | `search`、`library_search`、`web_search`、`github_search`、`answer`、`read_doc`、`catalog`、`doc_probe`、`repo_probe`、`repo_dependencies`、`link_stats`、`api_insight`、`pattern_insight`、`symbol_insight`、`call_insight`、`import_insight`、`mining_health`、`resolve`、`job_show` | `read` |
| 审核 | `review` | `list` 需 `review:read`，其余动作需 `review:write` |
| 反馈 | `submit_feedback` | `feedback:write` |
| 索引申请 | `request_repo_index`、`request_docs_index`、`list_index_requests` | `index:request` |
| 索引状态 | `index_request_status`、`wait_index_request` | `read` |
| 索引审批 | `review_index_request` | `index:approve` |

MCP 拒绝以工具结果形式返回 `unauthorized` / `forbidden`，不产生 HTTP 403；被拒绝的 scope 判定会写审计。

## 7. 租户边界

- 普通主体的读写都限定在自身 `tenant_id`；跨租户按未找到处理。
- `*` 只表示全局可见，不作为写入租户。平台管理员读取时可跨租户，写入则落到真实归属租户（`*` 归一为 `default`）。
- 反馈处置与任务取消按租户校验；审核流转也校验行归属，平台范围除外。
- Key 不单独携带租户，租户来自主体；同一主体的所有 Key 共享租户归属。

## 8. 管理接口约束

| 操作 | 约束 |
|---|---|
| 创建主体 | 调用者必须可管理目标租户；非平台管理员只能签发自身持有的角色，角色名必须已知；同一主体不得同时持有 `developer` / `kb_admin` / `sys_admin` 中的多个（平台角色豁免） |
| 签发 Key | scope 已知、不超过调用者 scope、不超过目标主体角色 scope；空列表继承角色；受默认与最大有效期约束 |
| 吊销 Key / 批量吊销主体 Key | 目标主体必须属于调用者可管理的租户；平台管理员不限租户 |
| 列举 Key | 普通管理员只看本租户；平台管理员看全部；明文不回显 |
| 配置刷新 | 需 `admin:config`；解析或重建失败返回错误并写审计 |
| 写入配额策略 | 需 `admin:config`；只能写自身可管理的租户；主体必须属于该租户；写后失效策略缓存 |

## 9. 配额与审计

配额按 `(tenant_id, principal_id, resource, period)` 计数，资源包括 `http_requests`、`index_requests`、`job_submissions`、LLM token 等。`http_requests` 由统一中间件拦截，业务资源由路由入口拦截；两者共用同一执行函数，拒绝返回 429，并写 `quota_events` 与审计。策略行经 `[admin:config]` 的配额接口读写，缺省回落到配置字段；配额数据访问失败时放行，属于可用性优先的取舍。

审计记录覆盖：`auth.denied`、`auth.forbidden`、`keys.list`、`keys.issue`、`keys.issue.platform`、`keys.revoke`、`keys.revoke_all`、`principals.list`、`roles.split`、`config.refresh`、`quota.policy`、`scrape.reject`、`scrape.submit`、`probe.reject`、`feedback.create`、`feedback.resolve`、`jobs.create`、`jobs.cancel`、`review.*`、`index_request.create`、`index_request.approve`、`index_request.reject`、`index_request.cancel`、`index_request.complete`、`index_request.auto_approve`、`quota`。含平台角色的签发单独记 `keys.issue.platform`，因为该角色是互斥的唯一豁免项。审计写入失败不阻断业务。

## 10. 特殊入口与未实现项

- `dev` 模式注入固定平台主体，跳过 Key 校验；只允许回环监听，不得经反向代理对外提供。
- `serve-mcp --stdio` 与 `--stdio` 给本机操作者注入平台主体；本机 CLI 直连数据库也属于可信运维路径，不经过 API Key 与审计。
- 未实现密码登录、会话、OIDC、MCP OAuth 2.1 与 per-tool scope；如对外开放 MCP，需要先补授权服务器与 scope 映射。

相关设计与修正记录：`docs/plan/permission-hardening.md`、`docs/plan/permission-boundaries-repair.md`、`docs/plan/permission-residual-tasks.md`、`docs/plan/role-separation.md`、`docs/app/config.md`、`docs/app/web.md`、`docs/app/mcp.md`。
