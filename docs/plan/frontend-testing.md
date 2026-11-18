# 前端测试：从零搭建与用例分层

`webui` 此前没有任何测试栈，`pnpm run check` 只做静态检查（类型、文案、设计令牌）。上一轮的前端修复（会话恢复、反馈门控、导航单源）只能靠类型检查与构建兜底——类型对不等于行为对。本文记录选型、分层与已落地用例。

## 0. 结论

| 项 | 结果 |
|---|---|
| 栈 | vitest 3 + @vue/test-utils 2 + happy-dom 19，复用应用自己的 Vite 管线 |
| 配置 | `webui/vitest.config.ts` 合并 `vite.config.ts`，`include: src/**/*.spec.ts` |
| 命令 | `pnpm test`（单次）、`pnpm test:watch` |
| 规模 | 8 个文件 59 例，全部通过 |
| 为可测性做的重构 | 路由守卫抽为纯函数；签发可用性判定抽为纯函数 |

## 1. 现状与缺口

- `webui/package.json:6-15` 的脚本只有 `dev` / `build` / `preview` / `check` / `gen:api` / `check:i18n`，没有任何测试入口（本轮新增 `test` / `test:watch`）。
- `webui/scripts/check-i18n.mjs` 与 `check-design-tokens.mjs` 是静态断言，不执行组件逻辑。
- 仓库根的 `e2e/check_llm.py` 是后端联调脚本，与前端无关。
- 因此 `stores/auth.ts` 的会话恢复分支、`router` 的守卫判定、`FeedbackView` 的 scope 门控在改动后没有任何自动化回归。

## 2. 选型

| 候选 | 取舍 |
|---|---|
| **vitest + @vue/test-utils + happy-dom**（采用） | 与 Vite 同源，直接复用 `@vitejs/plugin-vue`，`.vue` 与 `import.meta.env` 无需额外转换；happy-dom 比 jsdom 快且原生支持 ESM |
| jest + ts-jest | 需要为 ESM、`.vue`、`import.meta` 各配一层转换，与现有构建链重复 |
| Playwright / Cypress | 真浏览器行为最可信，但要拉起后端与种子数据；当前没有多环境 fixtures，投入产出差 |
| @testing-library/vue | 更贴近用户交互，但团队尚未确定该范式；`mount` 已能覆盖现有关键路径 |

实测环境可用性：`element-plus` 的 `el-menu` / `el-dialog` / `el-form` 在 happy-dom 下可正常挂载（`AppSidebar` 与 `FeedbackView` 都是真实挂载，未整体 stub）。

## 3. 分层原则

按"断言价值 ÷ 维护成本"排序，四层从高到低：

1. **纯函数**：权限判定、角色组合、导航表结构。无 DOM、无 mock，最快且最稳定。
2. **store 与请求边界**：`detectMode` 的三条分支、登录持久化。mock 掉 `api` 一层即可覆盖。
3. **组件挂载**：断言"给定 scope 渲染什么"，不测 element-plus 自身行为。
4. **不测**：样式数值、i18n 文案是否正确（`check-i18n.mjs` 已管）、后端契约（后端集成测试已管）。

为落进前两层，做了两处重构——都是把判定从组件/框架里搬出来，不改变运行时行为：

| 重构 | 原因 |
|---|---|
| `router/index.ts` 的内联守卫 → `router/guard.ts:15-31` 的 `authGuard(to)` | 直接 import 路由实例会绑到 `createWebHistory`，测守卫不必造路由 |
| `KeyIssueDialog` 的按钮可用性 → `utils/roles.ts:72-74` 的 `canIssueKey` | script setup 的内部状态不便从外部驱动，纯函数可直接覆盖 |

## 4. 用例清单

| 文件 | 例数 | 锁住什么 |
|---|---:|---|
| `src/utils/roles.spec.ts` | 12 | 组内两两冲突、三元全占、`external` 可叠加、平台豁免；可授予角色集合；提交可用性 |
| `src/stores/auth.spec.ts` | 10 | 有 key 先带凭据恢复（只发一次请求）、stale key 降级 dev、无 key 探测失败为 enforced、`ready` 后不重复探测、登录持久化、scope 门控 |
| `src/router/guard.spec.ts` | 11 | 匿名跳登录带回跳、public 放行、dev 不要求 key、缺 scope 跳 forbidden、`any` 任一满足、非 `any` 全满足、无主体时有 scope 要求回登录 |
| `src/router/nav.spec.ts` | 6 | 路径唯一、每项声明 scope、分组合法、`/admin` 与 `/feedback` 必为 `any`、路由 meta 与表一致、非导航页仍可达 |
| `src/components/business/AppSidebar.spec.ts` | 6 | 匿名无菜单；开发者无 review/admin；审核员无 ingest/admin；**仅持 `admin:config` 也能看到 admin 入口**（R3 回归）；平台全见；reader 不进侧栏 |
| `src/components/business/KeyIssueDialog.spec.ts` | 6 | 初始规则提示；无名称不可提交；冲突时提示并禁用；改回合法组合后恢复；只列出可授予角色；平台全列 |
| `src/views/FeedbackView.spec.ts` | 5 | 可读本人时加载列表并显示"仅本人"提示；**只有 `feedback:write` 时不发列表请求**（R2 回归）；审核员看全租户；无 `feedback:resolve` 不显示处置键；只给读者不显示提交表单 |
| `src/views/AdminView.spec.ts` | 3 | 仅持 `admin:config` 时无主体 tab 且**不发任何列表请求**（`if (!canKeys)` 缺 `.value` 的回归）；主体 tab 打开后请求 `/admin/principals` 并渲染冲突组与告警；无冲突时显示"正常" |

四条回归用例直接对应修掉的缺陷，改动若被回退会立即失败。

## 5. 运行

```bash
cd webui
pnpm test          # vitest run，一次跑完
pnpm test:watch    # 开发时增量
```

建议顺序（与后端门禁并列）：

```bash
(python) uv run pytest tests && uv run ruff check src tests && uv run mypy src
(webui)  pnpm test && pnpm check && pnpm build
```

## 6. 配置要点

- `vitest.config.ts` 用 `mergeConfig` 继承 `vite.config.ts`，因此 `@vitejs/plugin-vue`、`server.port`、`build` 配置只维护一份。
- `restoreMocks` / `clearMocks` / `unstubGlobals` 打开，避免 mock 在用例间泄漏。
- 测试文件与源码同目录、`.spec.ts` 后缀，`vue-tsc` 会一并做类型检查，测试代码不会成为类型洼地。
- 依赖 mock 用 `vi.hoisted` + 工厂，保留 `ApiError` 等真实导出（`FeedbackView.spec.ts:8-16`），因为组件会对它做 `instanceof`。

## 7. 限制

- **不是浏览器**。布局、滚动、焦点、真实事件序列都不可信；这些仍需人工或用 Playwright 补。
- **组件细节靠 stub**。`KeyIssueDialog` 把 element-plus 降为 `ElSelect` / `ElButton` 两个可驱动 stub，好处是稳定，代价是"真实组件的禁用态渲染"未被验证。
- **无覆盖率门槛**。当前目标是锁行为，不是追数字；若后续要设门槛，先确保 `webui` 的构建产物不被计入。
- **未覆盖的页面**：检索、洞察、入库向导、审核等页面仍无测试。它们的主要风险是表单与查询串，属下一批。

## 8. 待拍板

| # | 问题 | 选项 |
|---|---|---|
| T1 | 是否引入 Playwright 做真浏览器 e2e | 不引入（当前）/ 只覆盖登录与三个治理页 |
| T2 | 是否设覆盖率门槛 | 不设（当前）/ 设行覆盖下限 |
| T3 | 组件测试是否改用 @testing-library/vue 范式 | 维持 `mount` / 统一迁移 |
| T4 | 是否把前端测试接入 CI 与后端门禁同一任务 | 并列（当前建议）/ 独立流水线 |
