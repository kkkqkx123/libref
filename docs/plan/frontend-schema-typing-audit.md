# 前端 schema.d.ts 覆盖与弱类型现状审计

> 范围：`webui/src/api/schema.d.ts`（生成物）及其后端事实来源 `src/libref/web/api.py`。
> 目的：回答三个问题——当前 schema.d.ts 覆盖哪些功能、存在哪些弱类型、这些弱类型如何改进。
> 本文是现状审计，与 `frontend-schema-typing.md`、`frontend-wire-contract.md` 的关系：那两份记录
> 的是**已完成**的收紧（洞察行结构化、view 判别、置信度 high/low、列表状态、错误码闭合、
> 时间 ISO 化、多值参数数组化）；本文只审计**当前生成物中仍然存在的**弱类型，不重复已修复项。

## 0. 事实来源与核验方式

- 生成链路：`src/libref/web/api.py`（Pydantic 模型）+ `src/libref/web/app.py`（路由签名）
  → `tools/export_openapi.py` → `webui/openapi.snapshot.json` → `openapi-typescript`（v7.8.0）
  → `webui/src/api/schema.d.ts`。证据：`webui/scripts/gen-api.mjs:7-9,55-64`。
- 生成物头部声明不可手改：`schema.d.ts:1-4`。
- 消费封装：`webui/src/api/client.ts` 从 `paths` 推导路径/方法/查询/请求体/响应类型
  （`client.ts:37-116`），`webui/src/api/types.ts` 用 `Schema<K>` 从 `components['schemas']`
  派生别名（`types.ts:3-5`）。
- 统计口径：以当前签入的生成物为唯一事实；行号均指引生成物或后端源文件。

## 1. 当前 schema.d.ts 覆盖的功能

规模：42 条路径（`paths` 起止 `schema.d.ts:6-742`）、约 48 个操作（`operations` 起 `2516`）、
约 80 个 schema（`components.schemas` 起 `745`，止 `2508`）。按业务域归类如下。

| 功能域 | 覆盖的端点（节选，附行号） | 主要 schema |
| --- | --- | --- |
| 运维探针 | `/health:7`、`/readyz:560`、`/metrics:41`、`/mining-health:543` | HealthResponse / ReadyResponse / MetricsResponse / MiningHealthResponse |
| 身份与鉴权 | `/me:24`、`/admin/principals:629`、`/admin/keys:649`、`/admin/keys/{key_id}:667`、`/admin/principals/{principal_id}/keys:684` | PrincipalResponse / PrincipalSummary / ApiKeyRow / KeyCreate / IssuedKey |
| 配额与配置 | `/admin/quota/policies:701`、`/admin/config/refresh:725` | QuotaPolicy / QuotaPolicyUpsert / ConfigRefreshResponse |
| 目录浏览 | `/catalog:61`、`/pages:146`、`/resolve:458`、`/repo-deps:509`、`/link-stats:526`、`/web-search:475`、`/github-search:492` | LibraryResponse / VersionResponse / PageList / ResolutionResponse / DependencyResponse / PageLinkStats / VersionLinkStats |
| 检索与问答 | `/search:78`、`/library-search:95`、`/answer:112`、`/read:129` | SearchResponse / DiscoveryResponse / AnswerResponse / ReadResponse / SearchItem / AnswerCitation / ChunkLocator |
| 代码洞察 | `/api-insight:166`、`/pattern-insight:183`、`/symbol-insight:200`、`/call-insight:217`、`/import-insight:234` | ApiRankInsightResponse 等 13 个 `*InsightResponse` + 对应 Row |
| 审核 | `/review:251` | ReviewAction / ReviewItem / ReviewResponse |
| 入库申请 | `/index-requests:269` 及 pending/show/cancel/approve/reject（`287-371`） | IndexRequestCreate / IndexRequestItem / IndexRequestParams / IndexRequestResponse |
| 采集与探测 | `/scrape:372`、`/doc-probe:389`、`/repo-probe:406` | ScrapeRequest / DocProbeRequest+Response / RepoProbeRequest+Response |
| 任务中心 | `/jobs:423`、`/jobs/{job_id}:441`、`/jobs/{job_id}/cancel:612` | MineJobCreate / InduceJobCreate / MineJobDetail / InduceJobDetail / IndexJobDetail / JobSummary |
| 反馈 | `/feedback:577`、`/feedback/{feedback_id}/resolve:595` | FeedbackCreate / FeedbackItem / FeedbackResponse |

每个操作都声明了 200 成功、422 校验错误（GET 查询类）、default 拒绝三类响应；拒绝统一指向
`ErrorResponse`（如 `schema.d.ts:2535-2543`）。`webhooks` 与 `$defs` 为空（`743`、`2515`）。

结论：契约在**传输结构层面**（路径、方法、查询、请求体、响应信封、字段名、可空性）覆盖完整；
问题不在"漏覆盖"，而在**部分字段的值域类型过宽**。

## 2. 弱类型清单（当前仍存在）

把 150 处裸 `string` 按语义二分：`url/name/id/fingerprint/content/时间文本/自由文本` 等本就是
开放字符串，**不是弱类型**；真正的问题是"后端或领域层已有封闭取值域，却在契约里降级为 `string`"。
据此识别出 4 类现存弱类型。

### 2.1 已有 Literal 定义却未复用的枚举字段（最确定、优先修）

| 字段（schema.d.ts） | 后端定义 | 领域层封闭取值域 | 现状 |
| --- | --- | --- | --- |
| `CallRow.confidence:889`、`CallRow.resolve_kind:891` | `api.py:734-735` 裸 `str` | `astminer/models.py:9` Confidence、`11-28` ResolveKind（16 个值） | 降级为 string |
| `EvidenceCall.confidence:1098`、`EvidenceCall.resolve_kind:1100` | `api.py:633-634` 裸 `str \| None` | 同上 | 降级为 string |
| `ReviewItem.confidence` 已是枚举（`2197`） | `api.py:528` 用 `ConfidenceLevel` | 同上 | 对照组：证明可复用 |

同一取值域在 `ReviewItem` 是强类型、在 `CallRow`/`EvidenceCall` 是裸 string，属于同一契约内的
不一致。前端因此无法用类型区分 `AMBIGUOUS` 调用，只能靠运行期字符串比较
（`services/code/mining_read.py:148,198` 等大量 `confidence != 'AMBIGUOUS'` 的判断在前端无类型保护）。

### 2.2 有稳定取值集合但契约完全开放的 `kind` 字段

`schema.d.ts:1139`（FeatureNodeRow）、`1226`（ImportEdgeRow）、`1533`（KeyCreate）、
`1928`（PrincipalResponse）、`1943`（PrincipalSummary）、`2328`（SignatureRow）、
`2357`（SymbolRow）、`1161`（FeedbackCreate）、`1178`（FeedbackItem）均为 `kind: string`。
后端对应字段同样是裸 `str`（`api.py:355` KeyCreate、`657` FeatureNodeRow、`703` SymbolRow、
`719` SignatureRow、`749` ImportEdgeRow、`308` FeedbackCreate、`1060` PrincipalSummary 等）。
其中：

- `PrincipalSummary.status`（`schema.d.ts:1947`，后端 `auth.py:192,823`）实际只有 `active` 等
  有限状态，却是 string；
- `FeedbackItem.kind` 有默认值 `general`（`api.py:308`）但取值开放；
- `KeyCreate.kind` 默认 `human`（`api.py:355`），`roles/scopes` 为 `string[]`（`api.py:359-360`）；
  角色种类在 `role-separation.md` 中有明确定义，但契约里是自由字符串。

这些字段是否应收窄需逐个拍板（见第 5 节开放点），不能一概改成字面量联合。

### 2.3 通用 JSON（`JsonValue`）承载的结构化数据

`schema.d.ts` 中 `JsonValue-Output`/`JsonValue-Input`/`JsonValue` 共引用 24 处，其中值得关注的：

- `CallRow.args:896`、`EvidenceCall.args:1092`、`ApiArgRow.args:803` —— 调用实参；
- `SymbolRow.params:2363`、`bases:2365`、`param_types:2375`、`SignatureRow.params:2332` —— 符号签名要素；
- `ChunkLocator.merged_from:924/overlap_from:925` —— 分块来源；
- `IndexJobDetail.result:1253`、`InduceJobDetail.result:1460`、`MineJobDetail.result:1625` —— 任务结果；
- `scope` 字典（`1037`、`1293`、`1394`）—— 采集范围。

`JsonValue` 定义为 `boolean | number | string | JsonValue[] | {[...]} | null`（`schema.d.ts:2487-2489`）。
这是**合法的弱类型**：实参、任务结果、签名参数本质上是语言相关、无法在 REST 契约层穷举的自由形态
数据，`frontend-schema-typing.md:32-34` 已明确决定保留。前端通过 `field(row, key)` 动态取值
（`types.ts:44-46`，10 处使用点）。这类弱类型**不应强行消除**，只能在使用点做局部收窄。

### 2.4 索引字典 `[key: string]: T`（12 处）

`schema.d.ts:1085`（DocProbeResponse.errors）、`1602`（MetricsResponse.counters）、
`1798/1802`（PatternHealthRow.resolve_kinds/confidence）、`2047`（ReadyResponse.checks）、
`2488/2505`（JsonValue/ValidationErrorModel.ctx）等。这类是**开放映射**（key 为动态名称，
如错误码、指标名、置信度级别），`string` 索引符合语义；非缺陷，仅当 key 集合有限时（如
`resolve_kinds` 的 16 值）可升级为 `Partial<Record<ResolveKind, number>>`。

## 3. 弱类型的影响评估

| 类别 | 是否真实风险 | 说明 |
| --- | --- | --- |
| 2.1 枚举未复用 | **高** | 同一取值域在契约内强弱不一致；前端无法静态区分 AMBIGUOUS/EXTRACTED/INFERRED，靠字符串比较，拼错不报错 |
| 2.2 开放 kind | **中** | 需逐个判断：角色/状态类收窄有收益，反馈 kind 等开放式输入收窄会损害扩展性（与 `frontend-schema-typing.md:48` 对任务种类的判断一致） |
| 2.3 JsonValue | **低（合理保留）** | 自由形态数据，强转会失真；仅在使用点收窄 |
| 2.4 索引字典 | **低（符合语义）** | 开放映射，key 动态；个别有限 key 可升级 |

前端类型逃逸极少，反证了问题集中在契约层：`client.ts` 中 `any` 仅出现在路由字段名
（`router/nav.ts:15`，非类型逃逸），`@ts-expect-error` 全部在 `client.spec.ts:14-28` 用于验证
类型约束生效；错误码映射已用 `satisfies Record<KnownErrorCode, string>`
（`i18n/locales/zh-CN.ts:122`）做到穷尽检查。

## 4. 改进方案

原则（遵循 AGENTS.md:5-7 无向后兼容、:84-86 Pydantic 边界单一定义）：**所有收紧先从后端
Pydantic 模型改起，再重新生成 schema.d.ts 与 snapshot，前端只做派生适配**。不在生成物上手改。

### 4.1 收窄 confidence；resolve_kind 保持开放（已落地）

后端复用 `astminer.models.Confidence`，实际 diff：

```diff
# src/libref/web/api.py
-from libref.services.infra.jobs import JobKind
+from libref.astminer.models import Confidence
+from libref.services.infra.jobs import JobKind
-ConfidenceLevel = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]

 class ReviewItem(ContractModel):
-    confidence: ConfidenceLevel | None
+    confidence: Confidence | None

 class CallRow(ContractModel):
     ...
-    confidence: str
+    confidence: Confidence
     resolve_kind: str          # 保持开放，见下

 class EvidenceCall(ContractModel):
     ...
-    confidence: str | None
+    confidence: Confidence | None
     resolve_kind: str | None   # 保持开放，见下
```

效果：生成物 `schema.d.ts:892/1101/2200` 的 confidence 变为 3 值字面量联合，与
`ReviewItem` 对齐；删除 `api.py` 的重复 `ConfidenceLevel` 别名，收敛到单一来源
`astminer.models.Confidence`。前端 `CallInsightRow`、`PatternInsightRow` 自动获得窄类型。

**resolve_kind 不收窄（修正原计划）**：原计划把它收窄为 `ResolveKind`（16 值），但测试
`test_insight_rows_with_data` 暴露领域缺省值 `resolve_kind=""`（`astminer/models.py:79`
`resolve_kind: str = ""` 无默认值约束，`mining_read.py:1009`、`idiom.py:120` 也用 `or ""`
兜底）。空串是合法的"未分类"值，不在 16 值集合内；强行收窄会让响应校验失败。这与
confidence 不同——`CallInstanceRecord.confidence: Confidence`（`astminer/models.py:78`）
本身就是 Literal 且无空串缺省，故收窄安全。结论：resolve_kind 在传输层是开放 string，保持。

### 4.2 逐个拍板 kind 字段（见第 5 节，不在本轮一刀切）

- 角色/状态类（`PrincipalSummary.status`、`KeyCreate.kind`、`roles`/`scopes`）：在
  `services/infra/auth.py` 定义 Literal 并让 `api.py` 复用；前端 `roles.ts` 已有角色集合可对齐。
- 数据分类类（`FeatureNodeRow.kind`、`SymbolRow.kind`、`ImportEdgeRow.kind`）：先确认取值集合
  稳定再收窄；若来自 tree-sitter 节点类型（开放集合），保持 string。
- 反馈类（`FeedbackCreate.kind`、`FeedbackItem.kind`）：默认 `general` 但允许自由输入，
  与任务种类同理建议保持开放。

### 4.3 JsonValue 使用点局部收窄（不改契约）

在 `types.ts` 为常用实参形状定义类型守卫，例如 `isCallArgs(v: JsonValue): v is string[]`；
`field()` 调用点（`RequestApprovalsView.vue:63` 等）改为按已知字段取值。契约层保持 JsonValue 不变。

### 4.4 有限 key 字典升级（可选）

`PatternHealthRow.resolve_kinds/confidence`（`schema.d.ts:1798/1802`）的 key 若为固定集合，
后端改 `dict[ResolveKind, int]`（或 `dict[Confidence, int]`）生成 `Partial<Record<...>>`，
比 `[key: string]: number` 更强。需先确认服务层产出的 key 是否恒为该集合。

## 5. 开放点结论（已拍板）

1. **合并 ConfidenceLevel → Confidence**：已删除 `api.py` 的重复别名，`ReviewItem` 改用
   `astminer.models.Confidence`，单一来源。
2. **resolve_kind 保持 string**：领域缺省值为 `""`（`astminer/models.py:79`），不在
   `ResolveKind` 16 值内；收窄会拒绝合法数据，测试已证实。这是对本审计原假设的修正。
3. **PrincipalSummary.status / kind / roles / scopes 保持 string**：`ROLE_SCOPES`
   （`auth.py:22-61`）是 `dict[str, frozenset[str]]`，角色与 scope 可动态扩展；status 输出用
   `str(row[5])`（`auth.py:392,534`），无 Literal 约束。收窄会破坏扩展点，与"任务种类保持开放"
   同理。
4. **各类 `*Row.kind` 保持 string**：来自 tree-sitter 节点类型等开放集合。
5. **PatternHealthRow 字典保持 `[key: string]: number`**：key 来自 DB `GROUP BY resolve_kind`
   （`mining_read.py:635-637`），含空串等开放值，非恒定封闭集合。
6. **FeedbackCreate.kind 保持开放**：默认 `general` 但允许自由输入。

净结果：6 个开放点中仅第 1 项产生代码改动（confidence 三处字段 + 删重复别名），其余 5 项
经事实核查确认维持现状，本轮不改。

## 6. 验证方式

按 AGENTS.md:92-97 执行：

```bash
uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src
cd webui && npm run gen:api && npm run check && npm run build
```

`npm run gen:api` 重新生成 schema.d.ts 与 snapshot（两者同一次改动提交）；`npm run check`
含漂移检查（`gen-api.mjs:66-76`），确保生成物与后端契约一致。

本沙箱环境的注意点：`.python-version`=3.14 但 pyenv 未安装该版本，`uv` 经 pyenv shim 调用会
报 `version 3.14 is not installed`。落地时的可行做法：先 `uv python install 3.14`，
临时移走 `.python-version` 后用 `uv venv --python <3.14 绝对路径>` 建 venv 并 `uv sync`，
再恢复 `.python-version`；后续命令直接用 `.venv/bin/python`、` .venv/bin/ruff`、
` .venv/bin/python -m pytest`，避免 pyenv shim 拦截。前端依赖用 `npm ci`（已配置淘宝镜像源）。

## 7. 落地结果

已改动 3 个文件：`src/libref/web/api.py`（删重复 `ConfidenceLevel`、`ReviewItem`/`CallRow`/
`EvidenceCall` 的 confidence 改用 `Confidence`）、`webui/openapi.snapshot.json`、
`webui/src/api/schema.d.ts`（重新生成）。`uv.lock` 因镜像源差异被 uv 改写过，已还原，
与本次改动无关。

生成物变化：confidence 从 `string` 变为 `"EXTRACTED" | "INFERRED" | "AMBIGUOUS"`
（CallRow、EvidenceCall 可空），resolve_kind 维持 `string`。

验证结果：后端 `pytest` 全绿（780 passed、57 skipped）、`ruff check` / `ruff format --check`
/ `mypy` 全绿；前端 `npm run check`（含生成物漂移、类型、词条、令牌）与 `npm run build` 均通过。
过程中首次全量测试暴露 resolve_kind 空串问题（`test_insight_rows_with_data` 失败），据此修正
方案为不收窄 resolve_kind，重跑后全绿。

## 8. 与既有文档的关系

- `frontend-schema-typing.md`：已完成的洞察行结构化/view 判别/置信度 high-low/列表状态收窄，
  本文不重复；本文 4.1 的 confidence 是**调用置信度**（三值），与该文的**答案置信度**（high/low）
  是不同字段。
- `frontend-wire-contract.md`：任务种类、错误码、时间、多值参数四项已落地；本文审计未发现
  这四项的回归。
