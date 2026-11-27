# 图 gold 真实数据分析与代码侧落地

## 目标

合成 gold 已经覆盖五种模式与九种关系，但它只证明种子事实与查询结果自洽。真实数据 gold 要回答另一个问题：冻结语料派生出来的事实能否支撑同样稳定的图遍历判定。本文件记录可行性分析、数据缺口、候选路线与已经落地的代码侧方案。

## 权威来源

真实语料来自冻结归档 `tests/e2e/bench/fixtures/*`；派生事实来自 `tests/e2e/bench/data/bench.db`。数据库只能提供行级证据，不提供题面或期望值。最终 gold 必须人工对照事实行与派生规则写入；脚本只在忽略目录列候选和证据，不生成或回写真值。

当前冻结库规模为：`api_entities` 46,029 行、`call_instances` 99,149 行、`import_edges` 17,036 行、`doc_api_links` 198,449 行、`documents` 120,111 行、`seq_patterns` 273 行、`api_clusters` 158 行、`feature_nodes` 617 行、`doc_pattern_coverage` 1,922 行、`doc_link_edges` 0 行。

## 当前库不能直接覆盖全部关系

`entity_source_bindings` 把文档版本明确绑定到源码版本。例如 gradle 文档版本 34 指向源码版本 35，`doc_api_links.version_id=34` 的实体 ID 属于 `api_entities.version_id=35`。图查询强制单一版本，因此文档版本的强边无法解析到 API 节点，文档节点也不能作为 `resolve_entity` 的种子。

关系可用性如下：

| 关系 | 当前可用性 | 原因 |
|---|---|---|
| `call` | 可用 | 源码版本同时有实体与调用事实 |
| `import` | 可用 | 导入边与 API 实体同版本 |
| `inherit` | 实际不可用 | `bases` 存短名，派生要求完全限定名唯一；实测 gradle 继承边为 0 |
| `mentions` | 不可用 | 文档版本与 API 实体版本分离 |
| `pattern_next` | 不可用 | 序列模式只在文档版本，API 实体在源码版本 |
| `covers_pattern` | 不可用 | 模式覆盖在文档版本，且文档节点不可作为种子 |
| `member` / `parent` | 不可用 | 簇与特征事实在文档版本，缺少可解析 API 种子 |
| `doc_link` | 不可用 | 冻结库的导航边为空 |

## 三条路线

路线 A：只用当前冻结库做代码侧真实 gold。范围是 `call`、`import`、`neighbors`、`path`、`impact`、`usage` 与路径未命中。改动最小，证据全部来自现有事实，但覆盖不完整。适合先验证人工 gold 流程。

路线 B：从冻结夹具离线重放一份独立的 graph-gold 派生库，放在忽略目录，让文档与源码落在同一版本，并补跑链接抽取。这样能覆盖全部关系，且不改变生产图查询语义；派生库必须记录夹具、版本、profile、选行条件与原始库路径，不能冒充生产真相。

路线 C：让图读模型识别 `entity_source_bindings`，在文档版本事实中解析绑定源码版本的 API 实体。它最贴近冻结库现状，但会改变单一版本图视图契约，需要同步出口参数、OpenAPI、前端类型、文档与缓存键。没有明确产品决策前不实施。

推荐顺序是先 A、再 B、最后评估 C。

## 真实 gold 生成流程

候选脚本只读数据库，列出按版本与 profile 分组的调用、导入、继承、审核排除与证据行，输出写到 `tests/e2e/outputs/graph-gold/`。作者据此人工选择种子，逐条写出模式、方向、深度、关系过滤、期望稳定 ID、禁止节点、证据行与截断状态，并在 `source_ref` 中记录数据库、语料版本与行号。服务查询结果只用于最终复核，不得反向填充期望。

真实 gold 必须允许每条用例声明自己的作用域。合成 gold 可以沿用顶层默认值；真实 gold 的 gradle 与 httpx 用例分别绑定到不同库和版本，因此审核报告也要逐条打印作用域。

契约测试检查三条：一是每条证据引用指向真实行、版本与租户；二是用例作用域与 `source_ref` 一致；三是审核指标全部为 1、错误与截断异常为 0。冻结库重建后必须重新列证据并人工复核，旧行号不做兼容保留。

## 代码侧落地状态

路线 A 已落地：

- `tests/fixtures/graph_gold_real.json` 保存八条人工核对的代码侧用例，覆盖 gradle `v9.9.1` 与 httpx 源码版本，验证多作用域审核。
- `tools/graph_gold_evidence.py` 只列候选与证据，输出到忽略目录，不写 gold。
- `GraphQueryService.audit()` 支持逐条 `scope`，`tools/export_graph_audit.py` 在文本报告中打印逐条作用域。
- `tests/integration/test_graph_query_real.py` 在冻结库存在时校验事实行、作用域与审核指标；冻结库不存在时跳过。
- 缓存命中率仍不作为正确性指标，性能报告与本 gold 分开。

路线 B 与路线 C 未实施；相关缺口保留在本文件中作为后续决策依据。
