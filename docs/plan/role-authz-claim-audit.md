# 四类角色评估报告的逐条核查

> **基线说明**：本文的 `file:line` 引述对应提交 `8358877` 的工作树。此后两轮改动在 `services/infra/auth.py`、`web/app.py`、`cli/ops_commands.py`、`webui/src/views/AdminView.vue` 中插入了新代码（角色互斥、拆分命令、主体视图），这些文件的行号已整体后移，**以 `docs/plan/role-separation.md` 与 `docs/plan/permission-residual-tasks.md` 中的行号为准**。未受影响的文件（`web/middleware.py`、`mcp_server/server.py`、`db/schema.py`、`services/code/probe.py` 等）引用仍然有效。

## 0. 结论摘要

被核查文档（下称"原报告"）基于一个已被淘汰的代码基线：它引用的 `mcp_server/auth.py` 已被删除，`serve-web` 命令已不存在，角色表已从 4 个扩到 5 个，scope 注册表与密钥租户校验均已落地。按当前 HEAD（`8358877`）逐条复核 13 项实质问题：

- **不成立 6 项**：`serve-web` 缺中间件、`serve-mcp` 无法注入主体、密钥跨租户、scope 无注册表、身份表不在迁移、测试覆盖不足。
- **部分成立 3 项**：角色无互斥（已有防提权与索引禁自审）、探查面仅 `read`（文档探查已封 SSRF，源码探查仍开本地路径）、文档漂移（两处仍在，两处已修正）。
- **成立 4 项**：前端刷新丢会话、开发者反馈闭环断裂、`/admin` 菜单双口径、配额策略无管理出口。

原报告的定性方向（"scope 是唯一授权权威、入口应统一、前端只消费 scope"）与实现一致，但严重度排序已经失效：它列为 P0 的两条都已随 `docs/plan/permission-hardening.md` 落地消失，而它列为 P1-3 的前端三条至今无一条被修，且前端没有任何自动化测试兜住（前端三条已修，测试栈已补，见 `permission-residual-tasks.md` 与 `frontend-testing.md`）。

本文只做核查与处置建议，不含实施。

## 1. 核查基线

- 代码：`git clone --recurse-submodules` 经 `gh-proxy.com` 拉取，HEAD `8358877`（"add index request"），子模块 `crates/simvec`、`vendor/tree-sitter-groovy`、`vendor/tree-sitter-kotlin` 均已检出。
- 环境：Python 3.14.3 由 uv 安装（走 python-build-standalone 镜像），依赖经腾讯 PyPI 主源同步；crates.io 不可用已由 rsproxy sparse 索引绕开。
- 实测：`uv run pytest tests/unit tests/integration/test_authz.py` → **441 passed, 1 skipped**；`ruff check src tests` 与 `mypy src`（160 文件）均通过。原报告 §11 "未执行测试" 的空白已补齐，结论不再依赖静态推断。
- 判定口径：以当前代码可执行行为为准；原报告的 `file:line` 若与当前文件不符，先按内容重新定位，再判断问题是否仍存在。

## 2. 逐条核查

### 2.1 不成立的项（已在改造中消失）

| 原报告项 | 原判定 | 当前事实 | 证据 |
|---|---|---|---|
| P0-1 `serve-web` 无鉴权中间件 | 上下文断裂 | 命令已删除，只剩 `serve`；`build_web_app` 自身挂载中间件，请求号、主体、配额、指标都在中间件内完成 | `cli/parsers.py:445`；`web/app.py:135`；`web/middleware.py:159-228` |
| P0-2 `serve-mcp` 独立 HTTP 无法注入主体 | enforced 下功能死锁 | 只保留 stdio 形态，启动即注入本地平台主体；HTTP 仅由 `serve` 的 `/mcp` 提供，与 Web 共用中间件 | `cli/ops_commands.py:388-399`（注入 `:394`）、`:445-451`、`:485-489`、`:554` |
| P1-1 密钥管理跨租户 | 唯一隔离破口 | 列表非平台身份按租户过滤；签发与吊销都过 `can_administer`；集成测试固定该行为 | `web/app.py:1738-1747`、`:1818`；`services/infra/auth.py:316-317,410-411,438-439`；`tests/integration/test_authz.py:102,149` |
| P2-1 scope 无注册表 | 拼错静默失效 | `KNOWN_SCOPES` 由角色表并集导出，签发时校验未知 scope，目标角色边界同时校验 | `services/infra/auth.py:70,339-352,412-414` |
| P2-2 身份表不在迁移、请求热路径建表 | 每请求执行多语句 DDL | 身份表已并入迁移 23；`lookup_principal` 不再建表 | `db/schema.py:486-536,538-573`；`db/migrations.py:70`；`auth.py:467-508` |
| §6 测试覆盖不足 | 仅 5 例、无 HTTP 403 断言 | 主体单测 9 例、鉴权集成 8 例，含 `missing_scope` 403 与审计落库断言 | `tests/unit/test_principal_auth.py`；`tests/integration/test_authz.py:85-97` |

补充：原报告的角色映射表也已过期——`external` 现在还有 `index:request`，并新增了 `platform_admin`（`auth.py:26-68`），`dev` 注入点从中间件移到了认证入口（`auth.py:248-249`），带 scope 的路由从 34 条增至 39 条（`web/app.py` 内 `_need` 调用计数），MCP 只读工具从 19 个增至 21 个（`mcp_server/server.py:1287-1311`）。

### 2.2 部分成立的项

**P1-2 角色无职责分离** — "模型无法表达角色互斥"成立，"完全没有 SoD"不成立。已有限制：非平台身份不能授予自己未持有的角色（`auth.py:319-322`），索引申请禁止自审（`services/ops/index_requests.py:696-699`）。仍缺的是角色组合约束：`create_principal` 只校验角色名存在（`auth.py:295-299`），`sys_admin` 一旦被追加 `kb_admin` 即同时掌握采集与审核。

**P2-4 探查面只要 `read`** — 文档探查已封 SSRF：`/doc-probe` 调用 `validate_scrape_target`，拒绝非 http(s)、回环与私网、未列入 `allowed_hosts` 的目标（`web/app.py:1170`；`services/infra/tenant.py:150-203`）。源码探查仍开本地路径：`probe_repo` 对 `Path(source).is_dir()` 直接枚举（`services/code/probe.py:167-190`），而同一仓库对索引申请明确"本地路径只走 CLI"（`index_requests.py:119-121`）。两处策略相反，是真实不一致。

**P3 文档漂移** — 两处仍在：`docs/plan/multi-user-production-deferred.md:3,116` 引用的 `multi-user-production-deployment.md`、`multi-user-production-formal-implementation.md` 确实不在仓库；`docs/features/db/governance.md:5-19` 的 `jobs` 表未记录 `tenant_id`/`principal_id`/`cancel_requested`/`attempts`（对比 `db/schema.py:560-564`）。两处已修：`docs/app/web.md` 的"统一经顶层中间件"现在成立；`docs/app/mcp.md` 已改为"流式 HTTP 只由组合 `serve` 提供"。

### 2.3 成立的项

**P1-3(a) enforced 下刷新即掉登录态** — 成立。`detectMode` 固定以 `auth: false` 探测 `/me`（`webui/src/stores/auth.ts:35`），401 即 `me=null`（`:38-43`）；守卫在 `me` 为空且 scope 不满足时跳登录（`router/index.ts:31-49`），`localStorage` 里的 key 从未被回放（`api/client.ts:139` 只在 `auth` 未关闭时注入）。后果：每次刷新都要重贴一次 key。

**P1-3(b) 开发者提交反馈必见报错** — 成立。提交成功后无条件 `reload()`（`webui/src/views/FeedbackView.vue:139`），列表请求需要 `feedback:read`（`web/app.py:1613`），而 `developer` 没有该 scope（`auth.py:28`）。列表区也没有按 scope 隐藏（`FeedbackView.vue:31-44,183`），403 会覆盖掉成功提示。服务端列表只有租户维度、没有主体维度（`services/ops/feedback.py:42-93`），所以"给 developer 加 `feedback:read`"会直接放大为全租户可见——这一条不能只改角色表。

**P1-3(c) `/admin` 两套口径** — 成立。路由用 `any: true`（`router/index.ts:27`），侧栏用全满足（`AppSidebar.vue:64`）。在 key scope 可收窄之后（`auth.py:499-501`）该差异可被触发：只持 `admin:config` 的 key 能进页面却看不到入口。

**P2-3 配额策略有表无出口** — 成立，且比原报告更明确：`quota.set_policy` 有实现（`services/infra/quota.py:99-129`）、策略读取有缓存（`:159-202`），但全库无调用点，也没有对应路由或 CLI；策略只能直接写库，配置字段仅是兜底（`docs/features/web/permissions.md:20`）。表已随迁移 24 落地（`migrations.py:71`），不是半成品表，是半成品能力。

## 3. 原报告自身的引用质量问题

原报告数十处 `file:line` 中，与当前文件对不上的比例很高，且不是随机漂移，而是整体指向旧基线：

- 引用了**不存在**的 `mcp_server/auth.py`（§2.1 两处显式引用，另有多处 `auth.py:…` 按上下文指向该文件，如 `:166-210`、`:226-256`）。该文件在 `docs/plan/permission-hardening.md:40` 被列为删除项，`:55` 记录已完成。
- 同一模块的行号整体错位：`ROLE_SCOPES` 实际在 `services/infra/auth.py:26-68`（原报告称 18-27），`lookup_principal` 在 `:467-508`（称 166-210），`check_startup` 在 `:213-236`（称 24-40），`dev_principal` 在 `:511-521`（称 213-223）。
- 路由级行号整体偏移 30-200 行，例如 `/admin/keys` 签发实际在 `app.py:1772-1807`（称 1547-1581），`/feedback` 列表在 `:1606-1623`（称 1424）。
- 因此原报告"34 个带 scope 路由全部覆盖、无遗漏"这类以行号为准的结论无法复核，只能重新计数（当前 39 条）。

结论：原报告是按旧提交写成的评估，不是对当前代码的评估。它的问题清单在旧基线上大概率成立，但直接拿去安排工作会修掉已修的东西。

## 4. 处置建议

按"是否影响当前可用性"排序，前四项建议本轮处理。

1. **会话恢复（P1-3a）**：`detectMode` 改为分两段——有 key 时先用带凭据的 `/me` 恢复会话，成功即 `enforced`；失败再以无凭据请求辨认 `dev`；都失败才判定未登录。`auth.ts:32-43` 是唯一改动点，`router/index.ts` 无需改。
2. **反馈闭环（P1-3b）**：优先止血——列表区与 `reload()` 按 `feedback:read` 门控，提交后不再拉列表（`FeedbackView.vue:31-44,139,183`）。若要"提交者能看到处理结果"，需同时做两件事：列表接口增加主体维度过滤（`feedback.py:42-93`），以及一个只读本人范围的 scope 或在 `/feedback` 内按 scope 分流；只改 `ROLE_SCOPES` 会造成全租户反馈泄漏。
3. **`/admin` 口径（P1-3c）**：`AppSidebar.vue:64` 补 `any: true`。更彻底的做法是把菜单项与路由 meta 收敛成一份定义，由它同时生成路由与侧栏，消除两份复制（当前 16 条带 scope 路由与 15 条导航项各写一份）。
4. **配额策略出口（P2-3）**：能力已在，缺的是入口。补一对 `GET/PUT /admin/quota/policies`（`admin:config`），写入走 `quota.set_policy` 并调用 `quota.invalidate_policy_cache`（`quota.py:199-202`），同时写审计；若判断不需要按主体配额，则按 `AGENTS.md:5` 的"不留兼容"直接删表与 `set_policy`，不要让空表长期悬空。
5. **源码探查面（P2-4）**：与索引申请对齐——`/repo-probe` 与 MCP `repo_probe` 拒绝本地路径（`web/app.py:1226`、`probe.py:167-190`），本地枚举只留 CLI；若确实需要远程按路径探查，再引入独立 scope（如 `probe:run`）并只授予 `sys_admin`。
6. **角色叠加（P1-2）**：若接受"角色可叠加"，在 `docs/features/web/permissions.md` 写明自审风险与索引审批的兜底规则；若要表达互斥，在 `_validate_roles`（`auth.py:295-299`）加一张互斥表，命中即 `ValueError`，与现有 fail-fast 风格一致。
7. **文档收口**：删除 `deferred.md:3,116` 的悬空引用或补回被引文档；`governance.md` 的 `jobs` 表补齐 4 列；`permissions.md` 增补"配额策略当前只能改库"的现状（与第 4 项同步）。
8. **残留冗余（低）**：`ensure_auth_tables` 仍在反馈、审计每次调用时执行（`services/ops/feedback.py:32,48,105`、`services/ops/audit.py:24`）。表已入迁移，这些调用可收敛到启动期一次，避免每个反馈请求都跑一遍多语句 DDL。

## 5. 待拍板开放点

| # | 问题 | 选项 | 影响面 |
|---|---|---|---|
| O1 | 反馈对开发者是否可见 | 隐藏列表（改前端）/ 只看本人（需主体维度 + 新 scope）/ 全租户可见（仅给 `feedback:read`，不推荐） | 决定 scope 模型是否要引入"资源归属"维度，不只是前端改动 |
| O2 | 配额策略去留 | 补 `admin:config` 出口 / 删表删 `set_policy` | 与"按主体配额"的产品诉求绑定 |
| O3 | 源码探查本地路径 | 直接禁（与索引申请一致）/ 引入 `probe:run` | 仅禁则 CLI 能力不受影响 |
| O4 | 角色是否互斥 | 接受叠加（写清风险）/ 引入互斥校验 | 影响 `create_principal`、签发流程与角色表 |
| O5 | 菜单真源 | 只补 `any: true` / 路由与侧栏同源生成 | 后者一次性消除同类不一致 |
| O6 | `kind` 是否参与授权 | 维持标注 / 升格为授权维度（机器主体禁写反馈等） | 当前全库无授权分支读取 `kind`（`auth.py:84-94`，仅有 `/me` 回显） |

## 6. 验证口径

```bash
uv sync                                                    # 3.14 + 腾讯源
uv run pytest tests/unit tests/integration/test_authz.py   # 441 passed, 1 skipped
uv run ruff check src tests && uv run mypy src             # 均已通过
cd webui && pnpm run check                                 # vue-tsc + i18n + design tokens
```

前端改动需要手工三步回归（当前 `webui/` 无任何测试，仅类型与 i18n 校验）：带 key 刷新页面应停留在原页；`developer` 提交反馈后不应出现 403 错误块；仅持 `admin:config` 的 key 应在侧栏看到管理入口。

## 7. 一句话回答

原报告的"设计不合理"清单里，真正还站得住的只有四件事：**刷新丢会话、开发者反馈看不到也必报错、`/admin` 两套口径、配额策略没有管理入口**；其余六项是旧基线的历史问题，已在两轮权限加固中修掉，另三项是部分成立的策略取舍，需要在 O1/O3/O4 上拍板后才能定"是缺陷还是设计"。

## 8. 后续

以上成立项与部分成立项的处置方案与实施记录见 `docs/plan/permission-residual-tasks.md`。

两项后续跟进已单独落地：

- **角色互斥（§2.2 P1-2）** 按第 4 节第 6 条的建议在 `_validate_roles` 处落地组级互斥，平台角色豁免，见 `docs/plan/role-separation.md`。
- **前端自动化测试缺口（§0 末句）** 已补 vitest 测试栈与 56 例用例，见 `docs/plan/frontend-testing.md`。
