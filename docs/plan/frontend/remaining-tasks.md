# 前端剩余任务与实施细化方案

> 范围：`webui/` 相对 `docs/plan/frontend/`（`README.md`、`pages.md`、`api-mapping.md`、
> `components-state.md`、`ops-admin.md`、`phases.md`、`style-guide.md`、`i18n.md`）
> 尚未兑现的条目。只列差距与改法，不重复已落地结论；技术栈与托管方式不变。
> 前端五个阶段已完成；原开放点 O1–O5 已定策并连带做了四处后端补齐，
> 决策与错误码契约见 §11；第二轮复核查出的七条（R1–R7）及决策见 §12。

## 0. 核对基线

- 代码状态：`webui/` 61 个文件，`npm run check`（`vue-tsc` + 令牌脚本）与
  `npm run build` 在核对时均通过；五个阶段的页面与端点对接已成型。
- 结论：剩余缺口集中在四类——i18n 收口与门禁、深链状态回写、
  阅读与降级闭环、写面与引导的细节补全。没有需要重做的页面。

## 1. 已完成项（不再重复实施）

| 能力 | 落点 |
|---|---|
| 工程骨架、三元筛选器、主题、登录态 | `webui/src/stores/{scope,theme,auth}.ts`、`components/business/ScopeFilter.vue` |
| 路由与权限门控、SPA 回退 | `webui/src/router/index.ts`、`src/libref/web/spa.py` |
| 令牌单源与检查脚本 | `webui/src/styles/tokens.css`、`webui/scripts/check-design-tokens.mjs` |
| 五域页面（检索、洞察、入库、运维、治理） | `webui/src/views/` 17 个视图 |
| i18n 词条双语言与 Element Plus 语言包注入 | `webui/src/i18n/`、`App.vue` 的 `el-config-provider` |

## 2. 剩余任务清单

| 编号 | 问题 | 证据（改动前） | 阶段 |
|---|---|---|---|
| R1 | `useMessages()` 返回快照对象而非计算属性，切换语言不触发更新（文档明确禁止此写法） | `i18n/index.ts:48` 与 `i18n.md` §16 | 一 |
| R2 | 语言包动态导入重复：`zh-cn` 同时静态与动态引入，初始值绕开 `locale` 判定 | `i18n/element.ts:3`、`i18n/element.ts:14` | 一 |
| R3 | 语言切换后文档标题不更新，只在路由跳转时设置 | `router/index.ts:43` | 一 |
| R4 | 无词条门禁：键集合、空串、占位符、硬编码中文字符串均无检查 | `webui/package.json` 的 `check` 脚本 | 一 |
| R5 | 状态值原样输出，未走词条；中文标点硬编码在模板里 | `components/common/StatusBadge.vue:139`、`DegradedBanner.vue:9`、`views/IngestWizardView.vue:37` | 一 |
| R6 | 重复/死词条：`common.serviceNormal` 与 `header.serviceNormal`、`app.healthUnknown` 与 `header.serviceUnknown`、`header.goHome` | `i18n/locales/zh-CN.ts:75`、`:373`、`:371` | 一 |
| R7 | `useFormatter` 未被任何页面使用，且缺相对时间与后端时间格式解析 | `i18n/format.ts:4` | 一 |
| R8 | 搜索、问答、洞察、目录页只读 `route.query`，不回写，链接不可分享复现 | `views/SearchView.vue:57`、`views/ApiInsightView.vue:57` | 二 |
| R9 | 阅读器 404 无“未收录”提示与入库引导；窗口模式无前后块导航 | `views/ReaderView.vue:107`、`pages.md:29` | 三 |
| R10 | 问答页用 `answer !== ''` 伪造 `answerability`；429 未给配额重置与检索结果入口 | `views/AnswerView.vue:25`、`pages.md:22` | 三 |
| R11 | 顶栏健康点只区分 ok/unknown，未接 `/readyz`，就绪降级无全站提示 | `components/business/AppHeader.vue:22`、`ops-admin.md:39` | 三 |
| R12 | 任务列表不轮询运行中任务；权限判定在 setup 期取值，登录态变化后不刷新 | `views/JobsView.vue:86`、`views/JobsView.vue:119` | 四 |
| R13 | 审核 409 仅弹提示，不刷新该条当前状态，也不自动定位下一条 | `views/ReviewView.vue:86` | 四 |
| R14 | 反馈页忽略 `entity_id` 深链参数；洞察页转发反馈时把标识塞进正文 | `views/FeedbackView.vue:85`、`views/CallInsightView.vue:213` | 四 |
| R15 | 管理页指标只拉一次，无轮询；就绪检查不随标签切换刷新 | `views/AdminView.vue:159` | 四 |
| R16 | 入库向导缺 `/web-search`、`/github-search` 发现入口；探测参数只传地址。依赖查看最终落在目录页版本详情，不进向导 | `views/IngestWizardView.vue:123`、`:174`、`pages.md:74` | 五 |
| R17 | 目录页只能下钻搜索与问答，缺洞察入口 | `views/CatalogView.vue:25` | 五 |
| R18 | 文档漂移：开发期代理说法与实现不符；`gen:api` 实际产物名与 README 不一致 | `webui/vite.config.ts:8`、`webui/README.md` | 五 |

## 3. 阶段一：i18n 收口与门禁

目标：语言切换全站即时生效，词条结构纳入构建门禁，模板内不再出现硬编码中文标点。

改动点：

- `i18n/index.ts`：删除 `useMessages()`（与 `messages` 计算属性重复且非响应式），
  初始化时同步 `document.documentElement.lang`；`setLocale` 保持持久化语义。
- `i18n/element.ts`：`elementLocale` 改为 `Language` 类型，语言包走“首帧同步 + 其余懒加载”，
  同一语言包只进一次构建；异步回填后校验 `locale.value` 未被再次切换，避免竞态覆盖。
- `router/index.ts`：抽出标题解析函数，`afterEach` 与 `watch(locale)` 共用，
  语言切换即时改写 `document.title`。
- `i18n/format.ts`：改为 `useFormatters()`，提供 `number`、`percent`、`dateTime`、`relative`，
  内部解析后端 `YYYY-MM-DD HH:MM:SS`（UTC）与 ISO 两种文本；任务、审核、反馈、管理四处时间列改用它。
- 词条：新增 `status` 分组（任务、审核、反馈共用状态词条）与 `common.degradedTitle`、
  `answer.citationsCount`、`ingest.stepToProbe`、`ingest.stepToSubmit` 等函数式词条；
  删除 R6 列出的重复键。
- `components/common/StatusBadge.vue`：未知状态回落原文，已知状态取 `status` 词条。
- 门禁：新增 `scripts/check-i18n.mjs`（零依赖，借助 Node 的类型剥离直接读取 `.ts` 词条），
  校验键集合一致、类型一致、函数形参个数一致、无空串、无 `{n}` 占位符、
  `src/` 下除 `i18n/locales/` 外不出现 CJK 字符；接入 `check` 与独立 `check:i18n`。

验收：`npm run check` 通过；切换语言后顶栏、侧栏、页面标题、Element Plus 组件语言、
状态徽标同时变化；词条门禁能对删除键、空串、硬编码中文分别报错。

## 4. 阶段二：深链状态同步

目标：`components-state.md` 的“地址栏可复现状态”从口号变为实现。

改动点：

- 新增 `utils/query.ts`：`syncQuery(snapshot)` 以 `router.replace` 合并写回当前路由
  （保留 `lang`、`redirect` 等无关参数，跳过 `undefined`/空串/默认值），
  `applyScopeFromQuery(query)` 在挂载时把 `library`/`version`/`ecosystem` 灌入全局筛选器，
  `scopeSnapshot()` 读取当前筛选器。
- 检索链路：搜索（查询词、档位、条数、联想开关）、问答（查询词）、阅读器（地址、模式、
  位置、半径、来源组）在触发请求时回写。
- 洞察链路：五个洞察页的视图、API 名、多值标签、条数、偏移回写，挂载时按 URL 还原。
- 运维链路：任务详情的 `id`、目录的 `ecosystem`、反馈的 `status` 回写。
- 多值参数统一逗号串编码，与后端 `apis`/`pattern`/`containing`/`resolve_kinds` 语义一致。

验收：任一页面改动筛选后地址栏随之更新，复制地址在新标签页还原同一结果；
`lang` 参数在同步后仍在。

## 5. 阶段三：阅读与降级闭环

改动点：

- 阅读器：`404` 走“未收录”专属空态（提示未入库、给出去入库向导的按钮），
  其余错误仍走通用错误态；窗口模式按下发块的 `sort_order` 计算上一段/下一段中心位并可直接翻页。
- 问答：去掉伪造的 `answerability`，只按是否有正文判定可回答性；
  429 展示配额重置时间并提供“查看检索结果”出口（同一查询词与筛选条件跳搜索页）。
- 顶栏与全站：新增 `stores/health.ts`，`/health` 取指纹、`/readyz` 取检查项并固定间隔轮询；
  健康点区分正常/降级/未知，降级时在应用外壳顶部挂一条非阻断提示，列出未通过项。

验收：请求不存在地址得到未收录引导；窗口模式可在相邻块间移动；
配额拒绝时既有提示又有检索出口；停掉向量或 LLM 后顶栏变黄且提示原因，只读浏览不受阻。

## 6. 阶段四：运维写面闭环

改动点：

- 任务中心：列表在存在排队/运行中任务时轮询，全部终态即停；权限判定改为计算属性；
  详情 `id` 与地址栏双向同步（深链进来自动展开）。
- 审核：写操作 409 时弹出后端原文并重新拉取列表，避免乐观覆盖；
  处理后把焦点移到下一条，键盘连续处理不跳页。
- 反馈：读取 `entity_id` 深链参数，洞察页转发时把实体标识与类型一起带入；
  已解决项整条置灰；权限判定改为计算属性。
- 管理：状态标签页可见时按固定间隔轮询指标，切换标签或离开页面即停；
  就绪检查随标签进入刷新。
- 共享组件：`EvidenceList` 的反馈入口同样改为计算属性，避免 dev/enforced 切换后残留。

验收：运行中的任务列表自动更新；两条并发审核操作第二条得到 409 且列表状态与实际一致；
从调用页点“反馈”进入表单时实体类型与标识均已填好；管理页指标随时间变化。

## 7. 阶段五：入库引导补全与文档对齐

改动点：

- 入库向导：解析步骤并列三个发现入口（注册表联想、网页搜索、代码仓搜索），
  结果表支持一键填入名称与起始地址；探测步骤暴露文档探测的样本上限、页数上限、预过滤，
  仓库探测的子目录与样本上限；已入库版本依赖改由目录页版本行查看（`/repo-deps`）。
- 目录页：每个库行补充洞察入口（API、调用、模式），保留搜索与问答，
  下钻时同时写入全局筛选器。
- 文档：修正 `webui/README.md` 对 `gen:api` 产物与开发期代理的描述，
  使之与 `vite.config.ts` 和 `.env.development` 的实际行为一致。

验收：向导可从网页搜索或代码仓搜索结果直接落库；探测参数真实进入请求；
仓库依赖可查；目录页一跳进洞察即带库版本限定。

## 8. 跨阶段约束

- 新增界面文案必须同时补 `zh-CN` 与 `en`，否则阶段一的门禁失败。
- 不引入新运行时依赖；不新增全局状态，除 `stores/health.ts`（阶段三，具备明确归属）。
- 组件与页面不得直连 `store`/`services`；本目录改动全部位于 `webui/`。
- 每个阶段结束跑一次 `npm run check` 与 `npm run build`，不合并到一处再查。

## 9. 验收命令

```bash
cd webui && npm run check && npm run build
```

## 10. 落地结果

前端五个阶段与后端配合改动均已实施，`npm run check`（类型 + 词条 + 令牌）、
`npm run build`、`pytest`（新增 6 例）与 `ruff`/`mypy` 全绿；
无头浏览器实测：`?lang=en` 与运行时切到中文均即时更新壳层、页面标题与
`document.documentElement.lang`，选择写入本地存储；降级横幅随 `/readyz` 出现；
搜索执行后地址栏带 `query`、`preset`、`limit`；阅读未收录地址给出引导按钮。

上述表格中的行号是改动前的定位，修复后同一位置内容已变化。

| 阶段 | 实际改动 |
|---|---|
| 一 | 删除非响应式 `useMessages`；`elementLocale` 强类型化且语言包只进一次构建；标题随语言切换重写；新增 `useFormatters`（含 UTC 时间解析）并接入任务、审核、反馈、管理四处时间列；新增 `status` 词条组与 `language` 组；`scripts/check-i18n.mjs` 接入 `check` |
| 二 | 新增 `utils/query.ts`（`useQuerySync`、`applyScopeFromQuery`、`scopeSnapshot`）；搜索、问答、阅读器、五个洞察页、任务、目录、反馈、审核共十二处状态回写地址栏 |
| 三 | 阅读器未收录空态 + 入库引导 + 窗口前后段翻页；问答去掉伪造可回答性、429 转配额提示并保留检索出口；新增 `stores/health.ts` 与服务降级全站横幅 |
| 四 | 任务列表轮询 + 深链 id 双向同步 + 权限计算属性；审核 409 后重取列表并聚焦下一条；反馈读取 `entity_id` 且洞察页正确带入实体；管理页指标 10 秒轮询且随标签启停 |
| 五 | 入库向导补三个发现入口、探测参数（样本/页数/预过滤/子目录）；目录页补洞察下钻与版本依赖入口（`/repo-deps`）；`webui/README.md` 与托管文档对齐实现 |
| 六（后端配合） | 任务详情返回 `params`；版本项返回 `prerelease`；新增 `GET /pages`；错误响应统一带 `error` code；前端对应补“按原参数重提”、预发布标注与过滤、页面选择对话框、`ApiError.code` 与 `errorCodes` 词条 |

## 11. 剩余问题的设计决策与落地

原开放点 O1–O5 已逐条定策并实施，不再挂起。决策原则是**优先改后端补齐语义**，
而不是让前端猜测或复制后端规则。

| 编号 | 决策 | 理由 | 落地位置 |
|---|---|---|---|
| O1 | 任务详情补 `params`，前端只对 `failed` 且 `params` 非空的任务给“按原参数重提” | 参数本就入库（`jobs.params`），前端自存会与真实提交值脱节；重放成功任务会重复劳动，故限定状态 | `src/libref/services/infra/jobs.py`、`webui/src/views/JobsView.vue` |
| O2 | 不给 `/catalog` 加 `include_prerelease`，改为版本项返回 `prerelease` 标记，过滤放在界面 | 目录本就返回全部版本，服务端过滤会丢掉信息；预发布判定是后缀规则，只应在后端实现一次 | `src/libref/crawler/registry/__init__.py`（`is_prerelease`）、`src/libref/services/registry/libraries.py`、`webui/src/views/CatalogView.vue` |
| O3 | 新增只读 `GET /pages`，目录版本行打开页面选择对话框，选中带 `library/version` 跳阅读器 | 阅读器按 `url` 寻址，缺的是“有哪些地址”的枚举能力；产品面无需新页面 | `src/libref/services/docs/pages.py`、`src/libref/web/app.py`、`webui/src/views/CatalogView.vue` |
| O4 | 保留 Node 类型剥离方案，`package.json` 声明 `engines.node >= 22.6` | 零构建依赖即可读 `.ts` 词条；版本下限显式化后不再静默失败 | `webui/package.json` |
| O5 | 后端错误统一输出 `{"error": code, "detail": 原文, "request_id"}`，前端按 code 查表 | 状态码粒度过粗，但改 `detail` 会破坏旧客户端；增量加 `error` 字段两全 | `src/libref/web/errors.py`、`src/libref/web/app.py`、`src/libref/web/middleware.py`、`webui/src/api/client.ts` |

### 错误码契约

- 已知 code：`unauthorized/forbidden/missing_scope/origin_forbidden/not_found/page_not_found/job_not_found/quota_exceeded/review_conflict/invalid_limit/invalid_offset/invalid_scope/unavailable`。
- 未归类原因回落 `http_<状态码>`，前端此时展示后端 `detail` 原文，不吞信息。
- 新增错误分支时：在 `errors.py` 的 `_EXACT`/`_PREFIXES` 补映射，并在两份词条的 `errorCodes` 组补文案，
  否则该分支只会显示英文原文（可接受但不理想）。

### 仍需后续确认

| 事项 | 现状 | 影响 |
|---|---|---|
| 阅读器“未收录”页未列同版本其它页面 | 已可从目录选页，阅读器内未做候选推荐 | 取舍是避免阅读器再拉一次目录；若实测觉得绕，可在空态加一次 `/pages` 查询 |
| 预发布判定是后缀线索，非 SemVer | 与 `best_effort_stable` 共用同一套规则 | 极少数命名会被误标，属既有近似，未在本轮改动 |

## 12. 第二轮：剩余问题核对与设计决策

> §11 的 O1–O5 已落地验收。本轮重新通读 `docs/plan/frontend` 与实现代码，确认 §11 遗留两条
> 仍然成立，并查出五处“契约与实现不同步”。本章只定策、不改代码；每条给事实依据、备选方案
> 与决策，需要人拍板的集中在 12.4。

### 12.1 清单

级别定义：**P1** 影响正确性或与项目规范冲突；**P2** 影响可维护性、会随时间劣化；
**P3** 体验与工具链改善，可延后。

| 编号 | 问题 | 级别 | 事实依据 |
|---|---|---|---|
| R1 | 预发布判定不分生态、按子串匹配，NuGet 的连字符规则已丢失 | P1 | `src/libref/crawler/registry/__init__.py:295-298`（判定）、`:88-99`（线索表）、`:301-315`（兜底）、`crates.py:88-95`、`:512-513`、`src/libref/crawler/registry/nuget.py:51-53`（死代码）、`:87-95` |
| R2 | `GET /pages` 无响应模型，未纳入契约测试 | P1 | `src/libref/web/app.py:531-564`、`tests/integration/test_openapi_contract.py:17`、`AGENTS.md:84-86` |
| R3 | 错误码靠 `detail` 文本匹配，29 处 400 回落 `http_400` | P2 | `src/libref/web/errors.py:15-32`、`src/libref/web/app.py:100-115` 与其中 29 处 `detail=str(exc)`、`webui/src/api/client.ts:53-72` |
| R4 | 阅读器“未收录”空态无可选页面 | P3 | `webui/src/views/ReaderView.vue:33-38`、`webui/src/views/CatalogView.vue:151,216-276` |
| R5 | 429 只给重置时间文本，无重试入口，与文档承诺不符 | P3 | `docs/plan/frontend/api-mapping.md:69`、`webui/src/views/AnswerView.vue:20-25,102-104`、`webui/src/stores/notify.ts:10` |
| R6 | 中间件五处手写错误体，与 `error_body` 重复 | P2 | `src/libref/web/middleware.py:74-89,162-220`、`src/libref/web/errors.py:47-59` |
| R7 | `gen:api` 依赖在线后端，产物不入库也无漂移检查 | P3 | `webui/scripts/gen-api.mjs:9`、`webui/README.md:23`、仓库缺 `openapi.snapshot.json` 与 `src/api/schema.d.ts` |

### 12.2 逐条决策

#### R1 预发布判定按生态分派

**事实**。现行实现只有一个子串匹配：`any(hint in version.lower() for hint in _PRERELEASE_HINTS)`，
线索表为 `canary/experimental/dev/alpha/beta/preview/pre/rc/snapshot/nightly`，函数不带生态参数。
用 16 个典型版本实测，9 个判错：

| 版本 | 现行结果 | 应有结果 | 原因 |
|---|---|---|---|
| `1.0.0a1`、`1.0.0b2` | stable | 预发布 | PEP 440 用 `a/b` 段，无线索词 |
| `3.4.5-M1`、`2.0.0-EA` | stable | 预发布 | Maven 用单字母段 |
| `0.0.0-20240101` | stable | 预发布 | 日期式快照无线索词 |
| `13.0.1-patch1` | stable | 预发布 | NuGet 连字符规则已丢失 |
| `1.0.0-compressed`、`1.0.0-deprecated` | 预发布 | stable | 子串命中 `pre`/`rc` 却非预发布标记 |

NuGet 那条是重构遗留：`nuget.py:51-53` 定义了 `_is_prerelease`（连字符判定），但全仓无调用，
`pick_nuget_stable`（`:87-95`）直接复用通用兜底，而 `is_prerelease` 的 docstring（`:296`）仍写着
“NuGet adds its hyphen rule on top”，描述与实现已经脱节。

影响面有两层：直接可见的是 `/catalog` 的 `prerelease` 标记（`libraries.py:181,190`）驱动目录页
“仅稳定版”过滤与预发布标签；间接的是稳定版兜底——`include_prerelease=True` 时 pypi/npm/go
三个分支（`:375,389,415`）都用它重选，Maven（`:512-513`）与 crates 缺 `max_stable_version` 时也走它
（`crates.py:88-95`），会改变“默认入库哪个版本”。

**备选**。A 维持子串判定，接受上表 9 例错误；B 加生态参数并按生态分派，NuGet 规则收敛进公共函数；
C 引入 `packaging` 做 PEP 440/SemVer 判定，但 Maven、Go 仍无对应实现，判定会继续分裂，且新增运行时依赖。

**决策：B**。规则如下，全部在 `crawler/registry/__init__.py` 实现一次，其它模块只调用。

1. 签名改为 `is_prerelease(version, ecosystem=None)`；先去 build metadata（截断 `+` 之后，故
   `1.0.0+incompatible` 不算预发布）。
2. 按生态分派：`npm`/`crates`/`nuget` 为 SemVer 形态，连字符后有内容即预发布；`pypi` 匹配
   `(a|b|rc|alpha|beta|pre|preview|dev)\d*` 结尾段（`post` 不在其中，故 `1.0.0.post1` 仍是正式版）；
   `maven` 取分段 token 命中 `alpha|beta|rc|m|ea|snapshot|preview|dev|milestone`；`go` 认 `-pre`/`-rc`
   与伪版本 `vX.Y.Z-<14 位时间戳>-<12 位哈希>`。
3. 生态未知或为 `None` 时走 token 兜底：按 `[-._]` 切分，token 等于线索，或以线索开头且余下全是数字
   （`beta1`、`rc2` 命中；`prefix`、`compressed` 不命中）。
4. `best_effort_stable(versions, *, ecosystem=None)` 透传生态；`fetch_snapshot` 四处调用、
   `pick_nuget_stable`、crates 分支（`:375,389,415,512-513`、`crates.py:92`）一并传 `eco`；
   删除 `nuget._is_prerelease` 死代码，把连字符规则写进公共函数，docstring 同步改。

**影响面与风险**。这会改变部分版本“是否预发布”的结论，进而影响稳定版兜底结果。缓解：新增
`tests/unit/test_prerelease_rules.py` 覆盖上表 16 例加六生态边界，并做一次新旧结果差分（同一批
版本列表两套规则的结果对照表随改动附在提交里）；已有 `tests/unit/test_registry_providers.py:168-173`、
`tests/integration/test_registry.py:389`、`tests/integration/test_prerelease.py` 必须回归。

**验收**。`uv run pytest tests/unit/test_prerelease_rules.py tests/unit/test_registry_providers.py tests/integration/test_prerelease.py -q`；
`/catalog` 对 `1.0.0a1` 返回 `prerelease: true`，对 `1.0.0-compressed` 返回 `false`。

#### R2 为 `/pages` 补响应模型并纳入契约测试

**事实**。`app.py:531-564` 的路由直接返回 service 的 `dict`，没有 `response_model`；同文件只有
`/search`、`/answer`、`/read`、`/review` 四个端点带模型，而 `test_openapi_contract.py:17` 的
`_CONTRACT_PATHS` 恰好只校验这四条，并要求响应 schema 必须是 `$ref` 到对象。`AGENTS.md:84-86`
写明 REST 响应应为 pydantic 模型、不得字段漂移。`/pages` 是本轮唯一新增端点，属于新账。

**备选**。A 只补 `/pages`；B 一次性给全部 30 多个端点补模型；C 不补，靠前端 `types.ts` 手写。

**决策：A**。在 `src/libref/web/api.py` 增加 `PageRow`（`id/url/title/chunks`）与 `PageList`
（`library/version/total/limit/offset/items`），路由挂 `response_model=PageList`，并把 `/pages`
加入 `_CONTRACT_PATHS`。B 否决：30 多处改动与本次剩余问题无关，回归面大；C 否决：与 AGENTS 边界
模型要求直接冲突，且 `/openapi.json` 里没有 schema，前端类型生成拿不到依据。

**影响面**。响应体字段不变（模型字段与 service 字典逐一对应），OpenAPI 文档新增两个 schema；
`webui/src/api/types.ts:100-113` 的 `PageRow`/`PageList` 已与之一致，只需补一句注释指向模型名。

**验收**。`uv run pytest tests/integration/test_openapi_contract.py -q`；`/openapi.json` 中
`/pages` 的 200 响应为 `$ref` 到 `PageList`。

#### R3 错误码由“文本匹配”改为“抛点显式携带”

**事实**。`errors.py:15-32` 用 `(状态码, detail)` 精确或前缀匹配来反推 code，`app.py` 里
`detail=str(exc)` 的 400 共 29 处，全部回落 `http_400`；`client.ts:53-72` 对未知 code 直接展示
后端英文原文。后果有两个：改一句 detail 文案，code 会静默退化；新增错误分支要同时改 Python 映射表
与两份词条，漏改只表现为“显示英文”。

**备选**。A 在抛点显式传 `code`（自定义异常），文本匹配降级为兼容层；B 把 code 放响应头；C 维持
文本匹配长期使用；D 一次性迁移 29 处再上线。

**决策：A + 分批**。新增 `ApiRejection(HTTPException)`，构造时带 `code`，`app.py:100-115` 的
handler 优先读实例上的 `code`，取不到再走 `error_code(status, detail)` —— 存量 29 处零改动即可继续
工作。第一批只覆盖前端已有 UI 分支的五个码：`invalid_ecosystem`、`chat_disabled`、
`probe_target_invalid`、`upstream_unavailable`（502 目前回落 `http_502`）、`invalid_argument`
（其余 400 的通用码）。D 否决：29 处错误的成因分散在多个 service 的 `ValueError` 里，一次性分类
风险高于收益。

配套改前端：`ApiError` 增加 `detail` 字段，展示改为两级——主文案用 `errorCodes[code]` 词条，若
`detail` 非空且与词条不同，追加一行原文。理由：`invalid_argument` 这类通用码若不透出原文，会把
“到底哪个参数错了”这条唯一线索吞掉。

**影响面**。`errors.py`、`app.py`（handler 与五处抛点）、`client.ts`、`notify.ts`、`AnswerView`/
`ReaderView`/`JobsView`/`CatalogView` 的错误展示位、两份词条的 `errorCodes` 组。

**验收**。`tests/integration/test_web_error_contract.py` 现有 6 例全绿，并补 2 例（`ApiRejection`
的 code 优先于文本匹配；未知码仍回落 `http_<status>`）；`npm run check` 保证中英词条成对。

#### R4 阅读器未收录空态补候选页面

**事实**。`ReaderView.vue:33-38` 的 404 空态只有一句提示加“去入库”按钮；`/pages` 已可用，
目录页（`CatalogView.vue:151,216-276`）已实现选页对话框与翻页。

**备选**。A 维持现状，靠目录页选页；B 阅读器在 404 时按需查一次 `/pages`，列出同版本页面；
C 后端在 `/read` 的 404 响应体里带候选。

**决策：B，带触发条件**。仅当 scope 里 `library` 与 `version` 都在时才查，`limit=20`，只发一次请求；
请求失败静默忽略，不覆盖主提示与“去入库”入口。排序用“与未命中 URL 的最长公共路径前缀”仅作展示
顺序，不引入相似度打分——匹配规则一旦复制到前端就会与后端检索口径分叉。C 否决：404 响应体承载
业务数据会让错误语义膨胀，也让 `/read` 同时承担枚举职责，缓存与审计都变复杂。

**影响面**。`ReaderView.vue` 空态与一个惰性查询，新增 2 条词条（候选标题、空候选提示）。

**验收**。带 `library/version` 的未收录地址能看到候选并可点进；无 scope 时仍是原提示；
断网或 502 时候选区不出现、主提示不变。

#### R5 429 不做倒计时，改为可重试提示

**事实**。`api-mapping.md:69` 承诺“展示资源名与 reset_at，倒计时后允许重试”；实现只有
`AnswerView.vue:102-104` 记录 `resetAt` 并展示文本，`notify.ts:10` 在通知里拼一句重置时间，
没有任何定时器或重试入口；且只有问答页有专门区块，入库、任务、反馈的 429 只走全局通知。

**备选**。A 实现真正的倒计时并在到期后自动解禁按钮；B 不做倒计时，统一一个提示组件（资源名 +
本地化重置时间 + 手动重试按钮）；C 维持现状只改文档措辞。

**决策：B，并同步改文档**。倒计时需要客户端时钟对齐、每秒重绘，还要处理“倒计时归零但服务端仍拒”
的边界，收益只是视觉；而 `resetAt` 是 UTC 文本，`useFormatters` 已能本地化，配合一个重试按钮即可
覆盖真实需求。`api-mapping.md:69` 的措辞改为“展示重置时间并提供手动重试”，避免文档承诺超出实现。

**影响面**。新增 `components/common/QuotaNotice.vue`，替换 `AnswerView` 内联区块，并在入库、任务、
反馈三处的 429 分支复用；词条补“重新尝试”。

**验收**。四个写入口的 429 都出现同一形态提示，重置时间为本地时区；点重试复用原参数。

#### R6 中间件复用统一错误体

**事实**。`middleware.py` 有五处手写 `{"error", "detail", "request_id"}`（配额拒绝 `:74-89`、来源禁止
与未授权 `:162-182`、鉴权后端不可用与凭据查不到 `:202-221`），其中配额拒绝还手写了 `resource`/`reset_at`
提升；这些与 `errors.error_body`（`errors.py:47-59`，含 429 顶层提升）逻辑重复，两边改一处就会分叉。

**备选**。A 全部改调 `error_body`；B 保持手写，加注释说明；C 把手写逻辑抽成中间件私有函数。

**决策：A**。五处改为 `error_body(status, "detail 文本", request_id)`，429 处把 `resource`/`reset_at`
交给 `error_body` 的既有分支处理。行为不变，`test_web_error_contract.py` 现有 6 例作为回归网，
再补 1 例断言中间件拒绝与路由拒绝的响应同形（键集合一致）。

**验收**。`uv run pytest tests/integration/test_web_error_contract.py -q`。

#### R7 契约生成物入库并纳入门禁

**事实**。离线导出本身可用，但快照与 `schema.d.ts` 被忽略，`types.ts` 手写镜像 `api.py`，
`api<T>(path: string)` 接受任意路径与泛型强转；`npm run check` 不含契约校验。更严重的是
48 个操作里只有 5 个带命名响应模型，其余成功响应在 OpenAPI 中都是无字段对象。

**备选**。A 快照与生成物入库，客户端直接消费生成类型并加漂移门禁；B 保持离线生成但产物仅作
本地参考、不做门禁；C 删除生成链路。

**决策：A**（取代此前的 B）。后端为全部路由补 `response_model`，契约测试要求成功响应必须引用
命名对象 schema；快照与 `schema.d.ts` 入库，`gen:api --check` 在临时目录重生成后逐字节比对，
并纳入 `npm run check`；`api/client.ts` 从生成的 `paths` 推导路径、query、body 与响应类型，
`types.ts` 退化为生成类型别名。不采用正则解析 TS 做跨语言字段比对：生成类型已让 `vue-tsc`
在编译期完成同一件事，重复实现只会制造误报。

**验收**。后端未启动时 `npm run gen:api` 生成 42 条路径的快照；`npm run check` 校验生成物未过期；
`test_openapi_contract.py` 覆盖全部路由；前端字段改名或请求参数拼写错误由 `vue-tsc` 报出。

### 12.3 批次

| 批次 | 内容 | 理由 |
|---|---|---|
| 一 | R2、R6、R1 | 全是后端，一次 pytest 可覆盖，且 R2 顺带为 R7 提供验证样本 |
| 二 | R3（含前端两级文案） | 触碰错误展示面，需要前后端同时改并跑词条门禁 |
| 三 | R4、R5、R7 | 纯体验与工具链，可独立回退 |

每批结束跑：

```bash
uv run pytest -q
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src
cd webui && npm run check && npm run build
```

### 12.4 待拍板

| 事项 | 备选 | 建议 |
|---|---|---|
| Go 伪版本与日期式快照是否算预发布 | 算 / 不算 | 算。它们在语义上低于同号正式版，`go list -m -versions` 也会区分 |
| R1 改变判定后是否回溯已入库版本的 `stable` | 回溯 / 只影响新抓取 | 只影响新抓取。回溯需要重跑 registry 对账，收益不足 |
| R3 是否接受“词条 + 原文”两级文案 | 接受 / 只显示词条 | 接受。通用码若吞掉原文，参数级错误将无法定位 |
| R4 是否值得做 | 做 / 先量一次真实使用 | 先量。若“阅读器→目录→选页”路径实际可接受，可只保留 R5 的提示改善 |

### 12.5 落地结果

三批全部完成，`pytest` 706 passed / 57 skipped，`ruff`（含 `tools/`）与 `mypy` 全绿，
`npm run check`（类型 + 词条 + 令牌）与 `npm run build` 通过。

| 编号 | 实际改动 | 验证 |
|---|---|---|
| R2 | `src/libref/web/api.py` 新增 `PageRow`/`PageList`，`/pages` 挂 `response_model`，`_CONTRACT_PATHS` 增列 `/pages` | `test_openapi_contract.py` 2 例通过；`/openapi.json` 中该路径 200 为 `$ref` 到 `PageList` |
| R6 | `errors.py` 的 `error_code` 支持结构化 detail，`error_body` 增 `code` 形参；`middleware.py` 五处手写体收敛到 `_reject`（内部调 `error_body`） | 新增“中间件拒绝与路由拒绝同形”一例；`test_combined_serve`/`test_web` 全绿 |
| R1 | `is_prerelease(version, ecosystem)` 按生态分派 + `_token_hit` 边界匹配；`best_effort_stable` 透传生态；删除 `nuget._is_prerelease` 死代码；六处调用点补生态 | 新增 `tests/unit/test_prerelease_rules.py`（30 例）；27 个真实版本样本做新旧差分，7 个结果变化且全为“漏判→判对” |
| R3 | `errors.py` 增 `ApiRejection` 与 400 兜底 `invalid_argument`；handler 优先读实例 `code`；`/answer` 区分 `LlmConfigError`，502 分支改显式码；前端 `ApiError.detail` + “词条 + 原文”两级文案 | 契约测试 9 例（含 `invalid_argument`/`chat_disabled`/`upstream_unavailable`）；`npm run check` 保证中英词条成对 |
| R4 | `ReaderView` 在 404 且 scope 含 library+version 时查一次 `/pages`，按公共路径前缀排序取前 5，失败静默 | 类型与构建通过；无 scope 时不发请求；无头浏览器实测见下 |
| R5 | 新增 `QuotaNotice`，问答、任务、入库、反馈四处 429 统一用它，附手动重试；`api-mapping.md` 撤掉倒计时承诺 | `npm run check` + `build` 通过 |
| R7 | 快照与 `schema.d.ts` 入库，`gen:api --check` 纳入 `npm run check`；客户端消费生成类型，`types.ts` 改为别名；全部路由补响应模型并纳入契约测试 | `npm run check` 全绿；`test_openapi_contract.py` 覆盖全部路由；`vue-tsc` 能拦住字段漂移 |

**与 12.2 决策的三处偏差**（都是收窄，不是扩大）：

1. 未新增 `probe_target_invalid`。探测类 400 的成因分散在 `validate_scrape_target` 与
   `probe_docs_async` 的多个 `ValueError` 里，逐条登记会让码表膨胀；统一落到
   `invalid_argument` 并保留原文，前端两级文案已能定位到具体原因。
2. 400 兜底码定为 `invalid_argument` 而非继续 `http_400`。这样 29 处 `detail=str(exc)`
   无需改动即可被本地化，代价是原先断言 `http_400` 的用例改写为
   `test_generic_400_uses_invalid_argument`。
3. R6 的配额响应 `detail` 由字符串改为结构化对象，与路由层 429 同形
   （`{"error", "resource", "reset_at", "request_id"}`）；前端 `resetOf` 与 `detail` 解析
   两条路径都兼容，无迁移成本。

**仍需实机确认**：R4 的候选排序只按路径公共前缀，实际使用中是否会误导（若误导，退化为
不排序、按 URL 字母序列出即可）；R1 的 Maven 单字母 token（`-M1`、`-EA`）是否会把
某些正式版误标，需要在真实库版本上抽样。

**无头浏览器实测**（chromium + 桩后端，`dist/` 直接托管）：

- R4：`/reader?url=…/missing.html&library=demo&version=1.0` 的 404 空态出现
  “同版本其它收录页面（3）”并列出 `Guide intro` / `Install` / `API index`；
  原“该地址尚未收录”与“去入库向导”保持可见，候选区未顶替主提示。英文界面同形。
- R5：反馈页提交返回 429 后出现“配额已用尽”，重置时间按本地时区渲染为
  `2026/11/18 08:00:00`（后端给的是 UTC `00:00:00`），并给出“重试”按钮；
  不再只弹一条通知。
