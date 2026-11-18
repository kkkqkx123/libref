# 权限边界修正清单与实施方案

## 背景

权限体系已具备 API Key 单凭据、集中认证、角色 scope、租户隔离、配额与审计，但认证以外的策略判定仍分散在 Web、MCP、服务层与部署装配中。分散实现已经造成降权失效、跨租户资源操作和来源校验语义冲突。本文记录问题清单、分阶段修改内容与统一验证口径。

## 问题清单

| 编号 | 严重度 | 问题 | 证据 |
|---|---|---|---|
| 1 | 高 | Key 上的 scope 只能扩大主体的角色 scope，无法签发只读或受限 Key；最小权限不可执行 | `services/infra/auth.py` 中 `_principal_from_row` 先展开角色 scope，`lookup_principal` 再用 `\|=` 合并 Key scope |
| 2 | 高 | `feedback.resolve` 与 `jobs.cancel` 只按主键更新，缺少租户归属校验，可跨租户操作 | `services/ops/feedback.py:resolve`、`services/infra/jobs.py:cancel` |
| 3 | 高 | 平台管理员的 `tenant_id='*'` 既表示全局可见，又被原样写入业务行和审计，产生不属于任何真实租户的数据 | `services/infra/auth.py:bootstrap_admin`、`web/app.py:_tenant_of` |
| 4 | 中 | Scope 判定在 Web 内联、Web 依赖注入、MCP wrapper 与工具内部各写一份；MCP 拒绝无审计 | `web/app.py:_need/_read_guard`、`mcp_server/server.py:_deny/_wrap` |
| 5 | 中 | 来源校验分散在应用 Origin、MCP 传输安全与 CORS；MCP 传输安全在只配 Origin 时使用空 Host 白名单导致请求被拒；Auth 中间件在 OPTIONS 处短路使 CORS 预检失效 | `cli/ops_commands.py:cmd_serve`、`web/middleware.py:dispatch` |
| 6 | 中 | 配额检查、记账与审计在中间件和路由层重复实现，失败策略不一致 | `web/middleware.py:_quota_gate`、`web/app.py:_quota_gate` |
| 7 | 低 | 公开路径用前缀匹配放宽了 `/health*`；身份表 DDL 在迁移与运行时各写一遍 | `web/middleware.py:_public_path`、`db/schema.py` |
| 8 | 低 | 历史设计文档仍引用已移除的 `mcp_server/auth.py`、静态 token 与“无配额”等旧结论 | `docs/plan/multi-user-production-deferred.md` |

## 修改阶段

### 阶段一：统一策略原语

在 `services/infra/auth.py` 收敛 `missing_scopes`、`require_scopes` 与写入租户解析；`GLOBAL_TENANT` 只在 `services/infra/tenant.py` 定义。Web 的 `_need` 与 MCP 的 `_deny` 改用同一原语，中间件捕获路由抛出的 `AuthDenied` 并统一响应与审计。移除 MCP 工具内重复的 read 检查。

### 阶段二：Key scope 变为可收敛边界

Key scope 非空时取“角色 scope ∩ Key scope”；空列表保留“继承角色”语义，避免改变签发接口。签发时校验请求 scope 同时不超过签发者 scope 与目标主体的角色 scope。补降权与目标角色边界测试。

### 阶段三：补齐租户归属校验

`FeedbackService.resolve` 与 `JobManager.cancel` 接受可选租户参数，非全局租户按 `tenant_id` 条件更新。Web 写路径传入写入租户。平台管理员的全局可见性保留在读取路径，写入统一落到真实归属租户。

### 阶段四：统一来源与配额路径

MCP 传输安全在无显式 Host 白名单时从监听地址生成默认白名单；组合服务调整中间件顺序使 CORS 先于 Auth 处理预检；公开路径改为精确匹配。配额的判定、记账、事件与审计收敛到 `services/infra/quota.py` 的单一执行函数，两处调用方只负责各自的拒绝响应。

### 阶段五：消除重复定义

身份表 DDL 单源化，迁移脚本与运行时 ensure 共用同一字符串；审计写入统一归一化租户。同步修正历史文档的过期结论。

### 阶段六：验证

补充单元与集成测试覆盖：Key 降权与越界签发、跨租户反馈/任务拒绝、统一拒绝响应与审计、来源与配额收敛。统一执行测试、lint、格式与类型检查。

## 验证口径

- 单元测试：角色与 Key scope 交集、目标角色边界、写入租户解析、公开路径与 Host 白名单。
- 集成测试：强制鉴权下 scope 不足的响应与审计、跨租户反馈与任务操作被拒、组合服务的 CORS 预检。
- 未覆盖的行为不得依赖隐式兼容；所有新增约束由测试固定。

## 完成情况

- 阶段一：`services/infra/auth.py` 提供共用 scope 与租户原语；Web 与 MCP 统一使用，中间件负责统一拒绝响应与审计；MCP 工具内重复检查已移除。
- 阶段二：Key scope 非空时与角色 scope 取交集，签发时校验目标角色边界；引导 Key 改为继承角色 scope，避免降权为单一平台 scope。
- 阶段三：反馈处置与任务取消加入租户条件；平台管理员的全局可见性与写入归属租户分离，写入不再落到 `*`。
- 阶段四：MCP 传输安全的 Host 白名单由监听地址兜底；组合服务中 CORS 先于 Auth 处理预检；公开路径改为精确匹配；配额判定、记账、事件与审计收敛到 `quota.enforce`。
- 阶段五：身份表 DDL 单源化，审计写入统一归一化租户，历史文档补充现状更正。
- 阶段六：`uv run pytest`（743 passed，34 skipped）、`uv run ruff check src tests`、`uv run ruff format --check src tests`、`uv run mypy src` 全部通过。
