# 角色职责分离（SoD）：建模、校验与存量收敛

本文接续 `permission-residual-tasks.md` 的 R6。该轮判定"角色互斥不落地"，本轮改变结论并落地：新增组级互斥约束、存量巡检与前端前置拦截。范围仅限角色模型与主体创建链路，不含租户隔离、配额与 scope 收窄（见前篇）。

## 0. 结论

| 项 | 结果 |
|---|---|
| 模型 | 新增组级互斥：`developer` / `kb_admin` / `sys_admin` 在同一主体上**至多持一个**；`external` 不参与；`platform_admin` 豁免 |
| 校验点 | 收敛在 `_validate_roles`，因此 `create_principal` 的所有调用方自动受限，Web `/admin/keys` 返回 400 |
| 存量 | 不改写历史行；启动期告警 + `libref role-audit` 巡检 + `libref role-split` 显式拆分，浏览器侧 `GET /admin/principals` 可见冲突 |
| 前端 | 签发对话框改为受控多选，冲突即时提示并禁用提交；管理页新增主体 tab |
| 验证 | 后端 765 passed / 57 skipped，`ruff`、`ruff format --check`、`mypy`（160 文件）全通过；前端 59 passed，`check` 与 `build` 通过 |
| 边界 | 约束的是"一个主体不能兼任"，不约束"一个人持有两个主体"——这是模型外部问题，见 §9 |

## 1. 问题：缺口在哪个环节

现有两条校验都在纵向防提权，没有一条做横向防兼任：

- `_validate_roles`（`services/infra/auth.py:404-410`）只判断角色名是否已知。
- `create_principal` 的越权检查（`auth.py:430-433`）判断"调用者能不能授予某个角色"，即**不能给别人的比自己多**（`auth.py:431`）。

因此一个持有 `sys_admin` 的租户管理员可以创建 `roles=["kb_admin"]` 的主体，也可以创建 `roles=["developer","kb_admin"]` 的主体。后果按角色组合逐条列出：

| 组合 | 被抵消的设计意图 | 证据 |
|---|---|---|
| `developer` + `kb_admin` | 自提自审自决：同一人提交反馈并处置该反馈 | `feedback` 与 `review` 分表（`services/ops/feedback.py`）体现"反馈不污染审核"，在角色层被抵消 |
| `kb_admin` + `sys_admin` | 审核独立于运维：同一人既配置采集又审核知识条目 | `ROLE_SCOPES`（`auth.py:32-53`）同时含 `ingest:write` 与 `review:write` 的并集 |
| `developer` + `sys_admin` | 申请与审批合流：同一人申请索引又审批索引 | 两个角色都含 `index:request` / `index:approve`（`auth.py:30,39-40,50-51`） |

已有的局部兜底不足以覆盖：索引审批禁止自审（`services/ops/index_requests.py:683,698`），但那是"不能审批自己那一条请求"，不是"不能既申请又审批"；换个主体、换条请求即可绕开。反馈侧没有任何等价约束。

## 2. 语义决策

### 2.1 用"组"而不是"两两互斥"

三种组合的约束完全一致（三者任取二即冲突），两两列举会写出三条规则且容易漏。改为一个组，语义是"组内至多持一个"，规则表只有一项，新增第四个职能角色时改一行即可。

### 2.2 `platform_admin` 豁免

`platform_admin` 的 scope 是全集（`auth.py:54-72`），它在能力上必然同时覆盖提交、审核与运维。若纳入互斥，则：

- `bootstrap_admin` 无法创建（引导主体只持该角色，`auth.py:866`）；
- `dev_principal` 无法工作（`auth.py:845`）。

把平台角色排除在"租户职能"之外是唯一自洽的选择：它表达的是**跨租户范围**，不是**某个租户内的岗位**。这一点必须在文档里写明，否则容易被读成"给自己开后门"。代价是平台管理员仍可一人完成全流程，缓解手段是平台凭据的发放与审计（`audit_log` 记 `principal_id`），不是模型约束。

### 2.3 `external` 不参与

`external` 只有 `read` 与 `index:request`，与 `developer` 的差别仅在 `feedback:write`。它与任何角色叠加都不产生"自己审自己"的闭环，强行纳入只会让"给外部工具加一个反馈通道"这种合理诉求无处表达。

### 2.4 为什么不用配置表

互斥组若做成数据库或配置项，就要处理"规则改了以后存量主体怎么办"的二次问题，而当前没有任何多租户定制该规则的需求。硬编码在 `auth.py` 的常量里，配合 §4 的巡检，是投入产出最高的形态；真正需要按租户定制时再升级。

### 2.5 key scope 不能绕过

`issue_api_key` 的 scope 限定是**收窄**（`auth.py:665-678` 要求请求 scope ⊆ 调用者 scope 且 ⊆ 目标角色 scope），不是扩权。所以给一个 `developer` 主体的 key 请求 `review:write` 会被拒（`tests/unit/test_principal_auth.py` 的 `test_key_scopes_cannot_exceed_target_roles`）。互斥在主体层落地后，没有"用 key 把角色补回来"的通道。

## 3. 代码改动

| 位置 | 改动 |
|---|---|
| `services/infra/auth.py:83-86` | 新增 `ROLE_SEPARATION_GROUPS`（单组三元）与 `ROLE_SEPARATION_EXEMPT`（平台角色） |
| `auth.py:89-103` | `role_separation_violations`：返回每个超配组内的角色清单；命中豁免则直接返回空 |
| `auth.py:106-113` | `check_role_separation`：冲突时抛 `ValueError`，文案含具体组合 |
| `auth.py:404-410` | `_validate_roles` 内联调用，成为唯一校验入口 |
| `auth.py:443-475` | `RoleConflict`（TypedDict）+ `scan_role_conflicts`：只读巡检，不改写 |
| `cli/ops_commands.py:322-355` | `cmd_role_audit`：文本 / `--json` 两种输出，`--strict` 以退出码表达结果 |
| `cli/ops_commands.py:456-473` | `_warn_role_conflicts`：启动告警，异常吞掉不阻断 |
| `cli/ops_commands.py:492` | 在 `_prepare_auth` 末尾调用，覆盖唯一对外服务入口 `serve` |
| `cli/parsers.py:936-947` | 注册 `role-audit` 子命令 |
| `webui/src/utils/roles.ts:14-74` | 前端角色表镜像 + `roleConflicts` / `grantableRoles` / `canIssueKey` |
| `webui/src/components/business/KeyIssueDialog.vue:22-35,68-70` | 角色改为受控多选，实时提示，冲突禁用提交 |

错误语义：Web 侧沿用 `/admin/keys` 已有的 `ValueError → 400`（`web/app.py:1831-1832`），不新造错误码。理由是该路由的所有参数问题（未知角色、越权授予、越权租户）都走同一条 400 通道，单独为互斥造 409 会让前端多一个分支却无额外信息量。

## 4. 存量数据处理

升级前写下的行可能已经违规。三种处置对比过：

| 方案 | 问题 |
|---|---|
| 启动即拒绝 | 升级瞬间变成停机事故；违规是合规缺陷，不是运行时不安全状态 |
| 自动裁剪角色 | 静默削减权限，可能直接切断线上凭据，且无从追溯 |
| 只报告 | 需要人工判断拆给谁 |

选第三种，两个出口：

- 启动告警：`serve` 启动后若存在冲突，往 stderr 打一行并提示巡检命令（`ops_commands.py:456-473`）。
- 巡检命令：`libref role-audit`，文本模式逐行列出 `主体编号 租户/名称 roles=... conflicting=...`；`--json` 供脚本消费；`--strict` 在有冲突时退出码 1，可直接接 CI。

```bash
libref role-audit            # 报告，退出码恒为 0
libref role-audit --strict   # 有冲突则退出码 1
libref role-audit --json
```

实测（临时 HOME 造一条违规行）：文本模式命中 1 条并给出拆分建议，`--strict` 退出码 1，`--json` 输出结构化组信息。

## 5. 前端配合

`KeyIssueDialog` 原先用 `TagInput` 自由输入角色名，两个问题：拼错只有提交后才知道；冲突组合要等 400 才反馈。改为：

- 选项来自 `grantableRoles(me.roles)`——平台管理员看到全部五个角色，租户管理员只看到自己持有的（与 `create_principal` 的越权检查同构，`auth.py:430-433`）。
- 选择即计算冲突，冲突时显示"同一主体不能同时持有：A + B"并禁用提交；无冲突时显示常驻规则提示。
- `canIssueKey` 抽成纯函数（`roles.ts:72-74`），组件只负责绑定，判定逻辑可单测。

前端表是**镜像不是权威**：`roles.ts` 顶部注明服务端仍会重新校验，表漂移最多导致一次可避免的 400，不会造成越权授予。

## 6. 验证

```bash
uv run pytest tests                                   # 765 passed, 57 skipped
uv run ruff check src tests                           # All checks passed
uv run ruff format --check src tests                  # 283 files already formatted
uv run mypy src                                       # 160 files, no issues
```

新增用例：

| 文件 | 覆盖 |
|---|---|
| `tests/unit/test_principal_auth.py` | 四种冲突组合全部拒绝且不落库；`external` 与平台角色放行；巡检只报告不改写 |
| `tests/unit/test_role_separation.py` | 启动告警：无冲突静默、有冲突告警、表缺失不抛；拆分建议与装箱见 §10.2 |
| `tests/integration/test_authz.py` | `POST /admin/keys` 携带冲突角色返回 400 且不建主体 |

## 7. 拍板结论（本轮）

上表五项已定四项，O5 仍开放。

| # | 结论 | 理由 |
|---|---|---|
| O1 | **维持三元互斥**，不做配置开关 | 退到二元会让 `sys_admin`+`kb_admin` 合法，而这正是「运维自己审自己」最危险的一对；小队伍的正确解法是一人持两个主体，而不是放宽约束 |
| O2 | **保留平台角色豁免，但签发留痕** | 豁免不可去：引导管理员与本机操作主体都只持该角色，纳入互斥等于禁止引导。补偿是 `POST /admin/keys` 对含平台角色的签发单独记 `keys.issue.platform` |
| O3 | **不自动收敛，改提供显式命令** | 自动改写会静默吊销线上凭据。`libref role-split` 要求指定保留角色，支持 `--dry-run` 出计划，执行后把多余角色装箱到最少量的新主体并各发一把新 key（明文仅一次） |
| O4 | **补 Web 只读主体视图** | `GET /admin/principals`（`admin:keys`）返回角色、生效 scope 与冲突组；修复仍走 CLI，因为拆分要选保留角色并回显新明文 |
| O5 | 仍开放 | 需要「自然人」概念，超出当前主体模型 |

## 8. 文档同步

- `docs/features/web/permissions.md`：角色表补"互斥"标注；管理接口约束补互斥校验；新增巡检命令说明。
- `docs/plan/permission-residual-tasks.md`：R6 行的判定由"不引入互斥"改为"已引入组级互斥"，避免与本文冲突。
- `webui/README.md`：补前端测试命令与目录约定（见 `frontend-testing.md`）。

## 9. 边界与遗留

- **只管主体，不管人**。一人申请两个主体即可绕过；要真正约束"自然人"需要外部身份源（OIDC 主体绑定），当前模型不具备该信息。这一条必须在合规评审时说清楚，不能用角色互斥冒充完整 SoD。
- **平台角色是模型的例外**。它天然违反互斥，只能靠凭据发放纪律与审计兜底。
- **告警只覆盖 `serve`**。`serve-mcp --stdio` 注入的是本机平台主体、不读库内角色，故不告警；本机 CLI 属可信路径。
- **前端角色表需人工跟随服务端**。当前没有下发元数据的接口；若角色集合进入高频变更，应改为 `GET /admin/roles` 下发（O4 的自然延伸）。

## 10. 追改：四个结论的落地

§7 的结论需要代码承接，本节的增量即为此。行号对应当前工作树。

| 位置 | 内容 |
|---|---|
| `services/infra/auth.py:115-151` | `RoleSplitPlan` 与 `separation_advice`：每个冲突组按「保留哪个角色」枚举方案，附带两侧 scope，便于判断谁留下决策权 |
| `services/infra/auth.py:154-169` | `pack_roles`：贪心装箱，把迁出的角色压到最少的主体上 |
| `services/infra/auth.py:493-540` | `PrincipalSummary` 与 `list_principals`：按 `can_administer` 过滤，返回角色、生效 scope 与冲突组；纯读，不修复 |
| `services/infra/auth.py:543-563` | `set_principal_roles`：改角色复用创建时的全部校验（角色已知、组合合规、非平台调用者不得授予自身没有的角色） |
| `services/infra/auth.py:588-661` | `split_role_conflict`：`dry_run` 只出计划；执行时先整体校验再写，先建新主体再收窄原主体 |
| `cli/ops_commands.py:322-355` | `role-audit` 为每个冲突主体输出可执行的 `--keep` 选项 |
| `cli/ops_commands.py:358-412` | `cmd_role_split`：`--dry-run` / `--json`，执行后写 `roles.split` 审计 |
| `cli/parsers.py:949-971` | 注册 `role-split` |
| `web/app.py:1740-1760` | `GET /admin/principals`：只读视图，写 `principals.list` 审计 |
| `web/app.py:1834-1837` | 含平台角色的签发改记 `keys.issue.platform`，与普通签发区分 |
| `webui/src/views/AdminView.vue:52-91` | 「主体」tab：角色标签按冲突组标红，冲突组单列一列，顶部按冲突数告警 |
| `webui/src/views/AdminView.vue:180-196` | `loadPrincipals` 与 `roleSeverity` / `groupLabel` |

顺带修掉一个真缺陷：`loadKeys` 的守卫写成 `if (!canKeys)`，漏了 `.value`，`ComputedRef` 恒真，于是只持 `admin:config` 的管理员也会去拉密钥列表并收到 403 提示（`webui/src/views/AdminView.vue:171`）。`AdminView.spec.ts` 的第一条用例锁住它。

### 10.1 拆分的语义边界

- **只读不自动**：`role-audit`、`GET /admin/principals`、启动告警三条路径都不改写角色。
- **必须指名保留者**：`--keep` 强制人工选择，工具不替你判断「谁该保留审核权」。
- **新凭据只回显一次**：与签发口径一致；原主体的旧 key 仍然有效，只是权限收窄到保留角色，不会当场断线。
- **失败面收敛**：先整体校验（角色合法 + 调用者有权授予），再依次建主体、发 key、收窄原主体。顺序上「先建后收窄」意味着最坏结果是多出几个主体，而不是丢权限。
- **装箱不是「一角色一主体」**：`external` 这类只读旁观者会与保留角色同行，三元违规通常只需两个新主体而不是三个。

### 10.2 验证

门禁命令与结果同 §6（本轮复跑：后端 765 passed / 57 skipped，`ruff check`、`ruff format --check`、`mypy` 全通过；前端 `pnpm test` 59 passed，`check` 与 `build` 通过）。

本轮新增用例（11 条单测 + 2 条集成 + 3 条前端）：

| 文件 | 覆盖 |
|---|---|
| `tests/unit/test_role_separation.py` | 建议枚举不丢 scope、无冲突时建议为空、装箱最小化、`dry-run` 不写库、拆分后三方角色不重不漏、`--keep` 非法 / 无冲突 / 越权授予被拒、`set_principal_roles` 复用创建规则、`list_principals` 按租户过滤并标记冲突 |
| `tests/integration/test_authz.py` | `developer` 打 `/admin/principals` 得 403 且记 `auth.forbidden`；平台管理员可见冲突组与 scope；平台角色签发记 `keys.issue.platform` |
| `webui/src/views/AdminView.spec.ts` | 仅 `admin:config` 时不显示主体 tab 且不发请求；主体 tab 渲染冲突组与告警；无冲突时显示「正常」 |
