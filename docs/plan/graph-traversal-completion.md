# 图查询通道完整性与修改方案

## 背景

`docs/plan/graph-traversal-query-channel.md` 确立了独立图查询通道：节点与边从既有事实表惰性派生，不进入 FTS/向量 RRF 排名；出口只调用服务层。该文档同时归档了 Graphify、GraphRAG、cce 与 codebase-memory-mcp 的调研结论。本文件记录静态审查发现的完整问题清单与落地修改方案，供后续复核使用。

## 审查结论

通道边界、三出口、配置边界与强边口径已经落地；但原实现不能视为完整功能：

- 逻辑图字段缺少 `profile_id`、`tenant_id`、`evidence_ref`，API 节点的 `kind` 与设计的节点类型不一致；
- `profile` 未绑定到查询，profile 作用域的表按版本混读；
- 实体反查不带版本与租户，存在跨版本拼接风险；
- 模式边把模式成员边冒充种子邻接，能够产生假路径；
- 反向调用边丢失文件级调用点，影响面查询不完整；
- 功能树父链、同簇兄弟实体、文档模式覆盖与一致性证据未接入；
- 评测缺少独立 gold 集合；Web 的 OpenAPI 快照与前端类型未随 `/graph` 更新；CLI/Web 用户文档缺口。

## 问题清单

### 作用域与身份

1. 服务层只接受 `library`、`version`、`ecosystem`、`tenant`；没有 `profile`。`seq_patterns`、`api_clusters`、`feature_nodes`、`doc_feature_node_links`、`doc_pattern_coverage` 与 `doc_consistency` 都是 profile 作用域表，按版本混读会让支持度、社区上下文与一致性提示失真。
2. `version_id` 可以解析出多个版本行，原实现未拒绝，可能把同名实体在多个版本中的边拼到一起。
3. `_entity_by_name()` 与 `_api_row()` 只按名称或行号定位，不带版本和租户；继承、模式成员、簇成员、导入目标可能绑到别的版本实体。
4. 节点与边没有统一携带 `version_id`、`profile_id`、`tenant_id`、`evidence_ref`；`kind` 对 API 节点写成数据库的 `class` / `method`，与设计的 `api` 节点类型不一致。

### 图语义与遍历

5. 序列模式原来展开为 `pattern_member` 星形边。遍历时非当前节点的成员边被当作当前节点邻接，能够构造出并不存在的两实体路径，且结果边可能引用被裁剪掉的 pattern 节点。
6. 反向调用边在 `caller_entity_id` 为空时直接丢弃。真实挖掘数据里大量调用点只有文件与行范围，影响面因此失效。
7. `stdlib` / `builtin` 解析结果没有诊断展开路径；review `conflict` 保持排除符合保守口径，但需要明确保留。
8. `feature_context` 节点是死端，没有父链与成员回边；`api_clusters` 每行一个节点，只能回到自身成员，到不了同簇兄弟实体或代表成员。
9. `feature_nodes.parent`、`doc_pattern_coverage`、`doc_consistency` 未进入图，设计中的功能树上下文和文档证据可信度提示缺位。
10. 设计列出的 `parent`、`covers_pattern`、`pattern_next` 关系没有实现，实际关系集合与设计不一致。
11. BFS 的扇出截断不设置 `truncated`，最短路径不执行扇出上限，节点数恰好到上限时也会误报截断。
12. `usage` 模式接受 `depth` 但固定走一跳；路径查询忽略调用方传入的方向语义。

### 评测与契约

13. 只有冒烟集成测试，没有图通道 gold 集合、证据出处断言与审核导出。
14. `webui/openapi.snapshot.json` 和生成的 TypeScript 类型没有 `/graph`，前端契约漂移检查会失败。
15. `docs/app/mcp.md` 与配置文档已更新，但 `docs/app/web.md`、`docs/app/cli-client.md` 未列出图接口与图命令。

## 修改方案

### 作用域统一

- 服务层新增 `profile` 参数，按 `mining_profiles` 的既有口径解析 profile id；缺省为 `usage`。出口全部透传，CLI 增加 `--profile`。
- 查询必须给出非空租户；版本解析结果必须唯一，否则返回不可测量错误，不做跨版本拼接。
- 所有实体反查携带版本与租户；`GraphView` 自身也要求单一版本，防止绕过服务层拼图。
- `GraphNode` 与 `GraphEdge` 补齐 `version_id`、`profile_id`、`tenant_id`、`evidence_ref`。API 节点统一 `kind="api"`，源语言类别不再冒充节点类型。

### 图模型与遍历修正

- 移除 `pattern_member` 星形边，改为 `pattern_next`：只在种子确实是相邻对起点（或反向查询时是终点）时生成边，并把 support 写入权重与证据。
- `covers_pattern` 由 `doc_pattern_coverage` 派生，附带 `covered_kind`、support 和该文档在 `doc_consistency` 中的判定；`parent` 由 `feature_nodes.parent` 派生。
- `call` 边在反向遍历时允许 `caller_entity_id` 为空，回退到 `file:<version>:<path>` 节点并保留行范围。诊断开关可展开 `AMBIGUOUS`、`stdlib`、`builtin`；`conflict` 仍排除。
- 簇节点按 profile 与 cluster id 合并，邻居返回全部成员；特征节点返回父节点与关联 API。新增 `community` 与 `impact` 两个查询形态，分别走确定性社区上下文和反向影响面。
- 关系类型在遍历前校验；未知类型直接报错。BFS 与最短路径都执行扇出上限并准确设置 `truncated`。
- `usage` 保留一跳邻域语义，不再接受无效的 `depth` 改写；`profile` 透传给 `PatternReader`。

### 评测与契约同步

- 新增 `tests/fixtures/graph_gold.json`，覆盖调用路径、有序模式、强边文档、同簇兄弟与文件级影响面；集成测试逐条校验节点集合、关系类型与证据出处，不引入质量门禁。
- 重新导出 OpenAPI 快照并生成前端类型，使 `/graph` 与新增参数进入契约。
- 补齐 Web 与 CLI 用户文档，更新架构文档中的图查询说明和计划文档的落地状态。

## 验收口径

- 图查询不返回 `score`，不修改任何检索排序输入；现有检索排序测试保持原样。
- 默认查询不含弱文档边、歧义调用、冲突审核边与非目标解析结果；诊断展开由显式参数控制。
- 所有图节点与边都能追溯到版本、profile、租户和事实表行。
- gold 用例全部通过；契约漂移检查通过；Web、MCP、CLI 三出口使用同一服务签名。

## 落地状态

问题清单已全部闭环：

- `shortest_path` 接收调用方 `direction`，`path` 查询按指定方向约束边；`usage` 显式拒绝非空 `depth`，不再静默忽略。
- `GraphQueryService.audit()` 汇总 gold 用例的节点、关系与证据出处，只导出审核结果，不写入质量门禁。`tools/export_graph_audit.py` 提供离线导出入口。
- 集成测试覆盖路径方向、`usage` 参数拒绝、gold 报告、不匹配报告与导出工具。
- Web 与 MCP 文档同步说明 `path` 方向和 `usage` 一跳语义。

profile 解析不到时，服务仍会返回非 profile 事实表的图结果，节点与边的 `profile_id` 为空。这是 profile 事实尚未生成时的可查询状态，不做静默伪造；需要严格不可测量的调用方应先确认 profile 已建立。
