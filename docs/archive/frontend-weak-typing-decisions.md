# 前端契约弱类型保留清单与判据

> 记录 `webui/src/api/schema.d.ts` 中**刻意保持宽类型**的字段，以及判断依据与重开条件；同时记录
> 让行级类型真正生效的做法和已经收窄掉的契约面。定位以「`Schema.field`」与后端「`Class.field`」为准，
> 括号内行号是当前生成物与源码位置，重新生成后会漂移，改动前先按字段名确认。

## 1. 判定口径

- **不算弱类型**：`url`、`name`、`id`、正文、时间文本等本就是开放字符串，传输层没有可枚举值域。
- **算弱类型**：领域层或数据库已有稳定取值集合，却在契约里退化为 `string`。
- 本文只收录已逐项复核并决定保留的项；未复核的字段不在此列。

## 2. 保留判据

1. **有消费方。** 字段要么出现在视图的列声明里，要么被视图逻辑读取。只是"表格碰巧按运行时键渲染
   出来"不算消费方——那正是本文要修掉的形态，见 §3。
2. **收窄要从唯一来源出发。** 按 `AGENTS.md`「Pydantic at boundaries」，契约只能派生自后端
   Pydantic 定义；字段值若来自数据库文本列（`src/libref/db/schema.py` 里 `kind`、`status` 的默认值）
   或 tree-sitter 实体类型，写死字面量等于在传输层替数据库做约束。
3. **无消费方就删除。** 项目不做向后兼容，字段没有出口消费时保留宽类型没有意义，应走删除而不是
   继续挂着（§6 是已执行的删除，§7 是重开条件）。

## 3. 行级类型如何生效

`RowTable` 原先按首行的运行时键生成列（最多 8 列），行类型在 UI 侧退化成 `Record<string, JsonValue>`：
生成的 40 多个行 schema 在前端不承担任何类型职责，字段是否被使用也只能靠 grep 猜——这是此前判断
"某字段没人用"却判断错误的根源（`ChunkLocator.span_kind` 实际会被渲染成列）。

现在的做法：列由调用方声明，`RowTable` 只渲染声明的列；列名对行类型做编译期校验
（`webui/src/api/types.ts` 的 `ColumnKey<T>`，对联合类型逐支取键），拼错即 TS2322 并列出候选键。
7 处调用点（阅读页两张表、符号页、导入页、模式页、检索建议、入库探测）全部改成显式列。

由此确定：**列声明 + 视图逻辑引用 = 消费面**，不在其中且属于机器产物的字段进入删除评估。

## 4. 已收窄的对照基线

调用置信度 `confidence` 已从裸 `string` 收敛为三值 `EXTRACTED` / `INFERRED` / `AMBIGUOUS`：
后端三处字段共用 `astminer.models.Confidence`（`ReviewItem`、`CallRow`、`EvidenceCall`），
生成物对应 `schema.d.ts:888`、`1080`、`2191`。它能收窄是因为领域模型的该字段本身是 `Literal`
且没有空串缺省。

同族的 `resolve_kind` 没有跟随：领域默认值是空串（`astminer/models.py` 的
`CallInstanceRecord.resolve_kind`），空串表示"未分类"、不在 16 个枚举值内，收窄会让响应校验失败。
**有缺省值或由数据库文本列承载的字段，不参与收窄**，这条是下面所有保留决定的样板。

## 5. 保留清单

### 5.1 枚举类字段

| 字段（生成物位置） | 后端定义 | 值域事实 | 保留理由 |
| --- | --- | --- | --- |
| `CallRow.resolve_kind`（890） | `CallRow.resolve_kind` | 领域默认空串，空串合法 | 空串是"未分类"语义，不在枚举内 |
| `EvidenceCall.resolve_kind`（1082） | `EvidenceCall.resolve_kind` | 同上，另可为 `null` | 同上 |
| `FeatureNodeRow.kind`（1117） | `FeatureNodeRow.kind` | `feature_nodes.kind` 列直读 | 库内文本，特征树不按语言枚举 |
| `SymbolRow.kind`（2351） | `SymbolRow.kind` | tree-sitter 实体类型，跨语言开放 | 语言集未封闭，枚举随语法包变化 |
| `SignatureRow.kind`（2322） | `SignatureRow.kind` | 取符号类型，缺签名时为空串 | 存在空串缺省 |
| `ImportEdgeRow.kind`（1204） | `ImportEdgeRow.kind` | 导入类别按语言而异（`import`、`use`、`include`） | 语言相关，无法在契约层穷举 |
| `PrincipalResponse.kind`、`roles`、`scopes`（1922-1928） | `PrincipalResponse` | `ROLE_SCOPES` 是 `dict`，角色可动态扩展 | 角色表是扩展点，收窄会阻断新增角色 |
| `PrincipalSummary.kind`、`status`、`roles`、`scopes`（1937-1945） | `PrincipalSummary` | `status` 读出即 `str(row[5])`，列默认 `active` | 状态由数据库文本列承载 |
| `KeyCreate.kind`、`roles`、`scopes`（1525-1534） | `KeyCreate` | `kind` 默认 `human`，与列默认同值 | 只有一种取值时收窄属过拟合 |
| `FeedbackCreate.kind`（1139） | `FeedbackCreate.kind` | 默认 `general`，`min_length=1` 允许自由输入 | 反馈分类允许自由填写 |
| `FeedbackItem.kind`（1156） | `FeedbackItem.kind` | 同上传入值 | 同上 |

### 5.2 通用 JSON

`JsonValue` 三态别名（`schema.d.ts:1511-1516`、`2477`）承载语言相关、无法在传输层穷举的自由形态
数据，保持原样。字段级引用 18 处，按用途分四组：

| 用途 | 字段（生成物行号） |
| --- | --- |
| 调用实参 | `ApiArgRow.args:803`、`CallRow.args:895`、`EvidenceCall.args:1074`、`ReviewItem.args:2192` |
| 符号签名要素 | `SymbolRow.params:2355`、`bases:2357`、`param_types:2365`、`SignatureRow.params:2326`、`EvidenceCall.callee_params:1086`、`EvidenceRow.caller_params:1110` |
| 代码片段 | `EvidenceRow.snippet:1106`、`PatternSnippetRow.snippet:1881` |
| 任务结果与校验明细 | `IndexJobDetail.result:1231`、`InduceJobDetail.result:1450`、`MineJobDetail.result:1617`、`ValidationErrorModel.input:2489` |

另有三种 `dict[str, JsonValue]` 形态：`DocProbeRequest.target_filter:1019`、
`IndexRequestCreate.target_filter:1300`、`IndexRequestParams.target_filter:1407`——采集目标过滤器
由调用方自定义键，键集不可预知。

重开条件：某个视图开始按 `args` 的具体形状做分支（参数高亮、参数类型推断展示），
此时在**使用点**加类型守卫，契约不动。

### 5.3 索引签名

14 处 `[key: string]: T` 全部保留，按语义分四组：

| 语义 | 位置 |
| --- | --- |
| 开放映射（key 为错误码、指标名、健康检查名） | `DocProbeResponse.errors:1066`、`MetricsResponse.counters:1593`、`ReadyResponse.checks:2040`、`ValidationErrorModel.ctx:2494` |
| `JsonValue` 自身递归定义 | `1512`、`1515`、`2478` |
| 调用方自定义键的采集过滤器 | `DocProbeRequest:1019`、`IndexRequestCreate:1300`、`IndexRequestParams:1407` |
| 有限但含空串的键集 | `PatternHealthRow.resolve_kinds:1791`、`confidence:1795` |

`PatternHealthRow` 两处字典的键来自 `GROUP BY resolve_kind`，包含空串与未分类值，不是恒定封闭集合；
升级为 `Partial<Record<...>>` 的前提是数据库先补齐取值约束。

### 5.4 其余两项

| 字段 | 位置 | 事实 | 保留理由 |
| --- | --- | --- | --- |
| `ChunkLocator.span_kind` | 生成物 `914`，后端 `ChunkLocator.span_kind` | 四个常量：`exact_entities`、`enclosing_entity`、`group_fallback`、`document_range`（`chunking/budget.py`） | 阅读页作为文本列展示；值域常量定义在分块层，契约层要 Literal 就得跨层导出常量，收益仅限提示拼写 |
| `RepoProbeResponse.root_kind` | 生成物 `2079`，后端 `RepoProbeResponse.root_kind` | 探测得出的仓库根类型，拼进提示文案（`RequestsView.vue`） | 消费方式是纯文本插值，无分支判断 |

## 6. 已删除的契约面

原则：既不在列声明里、也不被视图逻辑引用，且属于机器产物的字段，从后端响应模型直接删除，重新生成契约。
`ContractModel` 的 `extra="ignore"` 让服务层多给的键自然丢弃，内部逻辑与存储不受影响。

| 模型 | 删除字段 | 判据 |
| --- | --- | --- |
| `ChunkLocator` | `source_group_id`、`merged_from`、`overlap_from`、`overlap_text` | 分组与合并派生元数据，无列、无逻辑引用 |
| `CallRow` | `start_byte`、`end_byte` | 字节偏移，前端只用行号 |
| `CallRow` | `call_order` | 调用序号，无消费方 |
| `ClusterRow` | `cluster_id` | 数据库行 id，前端按代表项与成员展示 |
| `FeatureNodeRow` | `parent`、`depth` | 层级已由 `path` 承载 |
| `SymbolRow` | `parent`、`depth` | 层级已由 `qualified_name` 承载 |

保留的标识符：`id`、`chunk_id`、`callee_entity_id`、`caller_entity_id` 等——它们是跨系统引用键，
不是可推导的展示字段，删除会切断外部按 id 回查的路径。

反例记录：`ChunkLocator.sort_order` 一度列入删除，但阅读页用它计算窗口前后页（`ReaderView.vue`
的 `deliveredOrders`），最终保留。说明判定必须同时看列声明与视图逻辑，只看列会误删。

## 7. 重开条件与复核方式

任一条件成立才重新评估保留或收窄：

- 视图新增该字段的列声明或逻辑引用（从"可删"变成"保留"，并需重新判断值域）；
- 服务端把取值改为封闭常量或 `Literal`，并作为单一定义向外暴露；
- 数据库为该列加上取值约束或非空约束（先解决空串与缺省值）；
- 生成物与后端契约出现漂移，先修复同步再谈收窄。

复核路径：`webui` 下 `npm run check:api` 查契约一致性；字段取值看数据库列默认值
（`src/libref/db/schema.py`、`db/postgres_schema.py`）与服务层直读处（`services/code/mining_read.py`）；
判断是否有消费方看视图的列声明与 `field()` 调用，不要用"页面能显示"倒推。
