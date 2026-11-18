# 权限残留问题：处置方案与实施记录

本文承接 `role-authz-claim-audit.md` 的核查结论，处理其成立与部分成立的七项：会话恢复、反馈闭环、导航口径、配额策略出口、源码探查本地路径、角色叠加说明、身份表 DDL 冗余，并一并收口文档。

## 0. 结论

七项全部落地，另加一项导航单源重构。判定口径与改法如下表，"影响"列说明不修会怎样。

| # | 问题 | 处置 | 影响 |
|---|---|---|---|
| R1 | enforced 下刷新丢会话 | 有 key 时先带凭据恢复，失败再辨认 dev | 每次刷新重贴 key |
| R2 | 开发者提交反馈必见 403 | 新增 `feedback:read:own`，列表按主体过滤，前端按 scope 门控 | 提交成功提示被错误覆盖，处理结果不可见 |
| R3 | `/admin` 路由与侧栏口径不一 | 导航表收敛为单源，权限元数据只有一份 | 收窄 scope 的 key 能进页面却无入口 |
| R4 | 配额策略有表无出口 | 新增 `admin:config` 下的读写接口 | 策略只能改库，配置字段成唯一实际入口 |
| R5 | 源码探查对 `read` 开放本地路径 | HTTP 与 MCP 入口拒绝本地路径，本地只留 CLI | 只读凭据即可枚举宿主机目录 |
| R6 | 角色可任意叠加 | 已由 `role-separation.md` 补上组级互斥，存量违规用 `libref role-split` 显式拆分 | 边界仍是"管主体不管人"：同一人持两个主体即可绕过 |
| R7 | 身份表 DDL 在请求路径重复执行 | 反馈与审计不再建表，依赖迁移 | 每次反馈与审计都跑一遍多语句 DDL |

## 1. 关键决策

- **反馈可见性取"只看本人"**：`developer` 增加 `feedback:read:own`，`kb_admin` 保留 `feedback:read`。理由是只做前端隐藏会让开发者永远看不到处理结果，闭环仍断；直接给 `feedback:read` 又会把全租户反馈放开。新增 `require_any_scope` 原语表达"两者其一"。
- **不复用 `feedback:read` 做主体过滤**：scope 表示"能看多远"，不由运行时猜；两个 scope 各自语义单一，签发时也能按需收窄。
- **导航单源**：路由与侧栏此前各写一份权限元数据，正是 R3 的成因。改为 `webui/src/router/nav.ts` 一张表，路由由它生成，侧栏按分组消费同一份。无分组的条目（如文档阅读页）只保留 URL 可达。
- **角色互斥（本轮未落地）**：当时四角色是 scope 预设包，叠加是既有语义；`platform_admin` 本身就要全量，强行互斥会先破坏引导管理员。后续 `role-separation.md` 用"平台角色豁免 + 组内至多一个"化解该矛盾并落地，R6 已随之更新。
- **配额策略写接口限 `admin:config`**：与配置刷新同权，不再新造 scope；租户管理员只能写自身租户，主体必须属于目标租户，避免"给别人的主体写一条永不生效的策略"这种脏数据。
- **本地路径只留 CLI**：与索引申请的既有口径（`services/ops/index_requests.py` 注释明确本地路径走 CLI）对齐，而不是新造 `probe:run`。远程按路径探查当前没有真实诉求。

## 2. 分阶段改动

### 阶段一：会话恢复

`webui/src/stores/auth.ts` 的 `detectMode` 改为三段：有 key 时先带凭据请求身份，成功即判定 enforced 并恢复 `me`；失败后做一次无凭据探测，成功即 dev 模式；都失败才判定未登录。守卫与登录页不改。

### 阶段二：反馈闭环

- 角色表：`developer` 增加 `feedback:read:own`。
- 原语：`services/infra/auth.py` 增加"任一 scope"断言，缺省全部时按 `missing_scope` 拒绝。
- 服务：`FeedbackService.list_items` 增加主体过滤参数，查询条件改为拼接式，替代原来的四分支。
- 路由：`GET /feedback` 按持有 scope 分流——有 `feedback:read` 看本租户，只有 `feedback:read:own` 时按主体过滤；主体编号缺失时返回空集而不是放开。
- 前端：列表区、筛选区与首屏加载都按可读 scope 门控；提交成功只在可读时刷新列表，避免 403 覆盖成功提示；仅本人可见时给出提示文案（中英各一条）。

### 阶段三：导航单源

新增 `webui/src/router/nav.ts`，导出带权限与分组的导航表；路由按表生成路由记录与 meta，侧栏按分组消费同一张表并做 scope 过滤。`AppSidebar.vue` 不再保存路径与 scope 副本。反馈入口的可读 scope 同时加入 `feedback:read:own`。

### 阶段四：配额策略出口

- 服务：`services/infra/quota.py` 增加策略列举，主体 `0` 对外呈现为空。
- 路由：`GET /admin/quota/policies` 列策略，平台管理员跨租户、普通管理员限本租户；`PUT /admin/quota/policies` 写入或覆盖，校验租户可管理与主体归属，写后失效策略缓存并记审计。
- 模型：`web/api.py` 增加写入请求模型，窗口限定三档，上限非负。

### 阶段五：源码探查

`services/code/probe.py` 的 `probe_repo` 增加"是否允许本地路径"开关，默认允许以保持命令行行为；网页与 MCP 两个入口显式关闭。关闭时命中本地目录直接按参数错误拒绝。

### 阶段六：DDL 冗余

反馈的提交、列表、处置与审计写入不再调用建表函数，改为依赖迁移已建表；认证侧的管理操作（建主体、签发、吊销、引导）保留调用，覆盖全新库的首启路径。

### 阶段七：文档收口

- `docs/features/web/permissions.md`：角色表、操作矩阵、路由表、管理约束、配额说明同步。
- `docs/app/web.md`：反馈读取语义与两个配额接口。
- `docs/app/mcp.md`、`docs/features/probe-and-scope.md`：本地路径只对命令行开放。
- `docs/features/db/governance.md`：`jobs` 表补齐租户、主体、取消标记与重试次数。
- `docs/plan/multi-user-production-deferred.md`：删除两处指向仓库外文档的引用，改指现存方案。

## 3. 验证口径

- 单元：角色展开含新 scope、探查本地路径开关。
- 集成：开发者只读本人反馈、审核者读全租户、只读角色被拒；配额策略的越权写入、跨租户写入、写入后生效与审计。
- 全量：`uv run pytest`、`uv run ruff check src tests`、`uv run mypy src`；前端 `pnpm run check` 与 `pnpm run build`。

## 4. 完成情况

阶段一至七均已完成。验证结果：

- 后端：`uv run pytest tests` → 745 passed、57 skipped（含新增的反馈可见性、配额策略、探查本地路径用例）；`ruff check`、`ruff format --check`、`mypy src` 全部通过。
- 前端：`pnpm run check`（类型、文案、设计令牌）与 `pnpm run build` 通过；会话恢复、反馈门控、导航单源均随构建验证。
- 既有用例 `test_probe_routes` 原以本地目录走 HTTP 探查，按新口径改为断言拒绝，限定与采样行为下沉到服务层直接验证。

## 5. 遗留与后续

- 角色互斥（R6）已由 `role-separation.md` 落地为组级约束；仍存在的边界是"只管主体不管人"，一人两主体可绕过，需外部身份源才能闭合。
- `Principal.kind` 仍只作标注，不参与授权；机器主体的差异化限制尚无诉求。
- 前端仍无单元测试，会话恢复与反馈门控靠类型检查与手工回归兜底，若前端继续扩大，建议补最小测试栈。
