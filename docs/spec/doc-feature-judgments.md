# 人工判定集与图 gold 编写规范

本规范适用于两类版本化真值：`tests/e2e/fixtures/doc_features/questions.json`、`drift_samples.json`，以及 `tests/fixtures/graph_gold.json`。最终判定必须逐条人工核对并手写；脚本只能列证据或在忽略目录中产出探索候选，不得生成、补全或回写最终判定。归档物化生成器已删除，不得重新引入；旧格式数据不做兼容保留。

## 1. 数据来源与职责

- 文档原始正文 `tests/e2e/bench/fixtures/*/docs/pages.jsonl.gz` 只读，查看时解压到系统临时目录，不把解压结果写入版本库。
- 文档派生证据 `tests/e2e/bench/data/bench.db` 只提供可核验事实：`documents.metadata.path`、`doc_features`、`doc_api_links`、`api_entities`、`doc_feature_node_links` 与 `doc_consistency`。题面文字只来自原始正文。
- 图事实的权威来源是 `tests/integration/test_graph_query.py::_seed` 中逐行插入的事实表；`tests/fixtures/graph_gold.json` 只保存人工核对后的期望，不得由 `GraphQueryService.query()` 的输出直接回填。
- `tests/e2e/bench/judgments.json` 与 `tests/e2e/generate_doc_feature_questions.py` 只作文档候选探索；候选池条目不得直接进入 `questions.json`，实体与特征不得从候选或词表自动填充。
- `tools/export_graph_audit.py` 只把 gold 与被测库的结果渲染成 JSON 和 Markdown，不修改 gold，也不把命中率写成通用门禁。

## 2. 文档特征判定集

### 2.1 选块

先确定题目要验证的文档块：从派生表取 URL、完整标题路径、块特征、实体边、节点父链与一致性判定，再回到原始正文定位同一标题章节。同名标题存在多个块时，以派生表该行的完整路径和特征为准，不用页面第一处匹配兜底。题面、特征、实体与判定必须指向同一块。

无合格正文证据的块不选；导航节点、页脚、目录列表、脚本或表格表头不作为题目来源；正文被归档截断后只剩菜单片段的块同样不选。

### 2.2 题面与实体

题面由人根据章节原句写出自然语言问题，直接问出该句说明的事实，不用“标题 + 首句”拼接，不在单词或数字中间截断。题型按语义选择：示例、参数或返回、表格、变更说明、漂移、无答案、限定名说明或普通语义题；不得仅因特征桶出现数字就写变更题。

实体只有同时满足三个条件才写入：题面明确点名该符号；目标文档的 `doc_api_links` 有该实体；源码版本绑定后的 `api_entities` 存在该限定名。只出现普通英文词且恰好与源码短名相同不算点名。实体取题面实际询问的最小集合，不为了对齐率追加。题面无合格实体时写空数组。

### 2.3 锚点、标签与划分

`expected_facets` 的块类型、标题深度、长度分桶、代码比例、表格标记、版本标记、示例语言、示例符号、参数名、返回值和废弃标记从派生表复制并人工核对；`expected_facets.heading` 只存 `metadata.path` 的末段，作者必须同时核对完整路径。`source_ref` 摘要由语料标识、URL 与完整标题路径计算。

`expected_verdict` 只在题面确为 drift 且目标文档结论唯一时写入；其他题型写空。`expected_feature_nodes` 从目标文档的父链复制，节点不完整的题不写。无答案题必须选目标块没有 `doc_api_links` 的页面，题面里没有别名不能当作无答案依据。`id` 保持语料前缀与两位序号；同一 URL 的题目必须全部落在同一 dev/test 划分。修改题面不改动 URL 与划分，除非该 URL 的证据本身不成立。

### 2.4 漂移样本

真 drift 必须从原始正文定位包含弃用、移除、替代或不再支持含义的句子，确认它指向具体 API、参数、字段、配置或行为。`human_verdict` 写 `drift`，`reason` 一句话指出原句与替代关系，不能只写“命中 deprecated”。题面必须包含同一弃用句的核心语义。

通用政策、否定式问答、生命周期枚举、变更枚举值、安全基线、构建流程术语以及只提 breaking changes 但未给出具体对象的泛化说明写 `not_drift`，并在 `reason` 中说明原因。`auto_verdict` 只记录自动判定观察值，即使与人工结论一致也不能省略原文核对。`corpus`、`url`、`heading`、两种判定与 `reason` 必填；样本应覆盖易误报形态，样本不足时只报告数量，不修改检索门禁。

## 3. 图遍历 gold 用例

### 3.1 权威事实与期望边界

每条 gold 必须指向 `tests/integration/test_graph_query.py::_seed` 中的具体事实行，并在 `source_ref` 里写明如 `tests/integration/test_graph_query.py::_seed:call_instances#1` 的定位。期望种子、节点、关系、证据与截断状态由人对照这些行与派生规则写出，不由查询结果反填。

`expected_seed`、`expected_nodes` 与 `forbidden_nodes` 使用稳定节点 ID，不使用展示标签。`expected_relations` 只能取 `GRAPH_RELATIONS` 中的关系类型；`direction`、`depth`、`relation_types` 与 `include_ambiguous` 是查询输入，必须显式写入需要它们的用例。路径用例必须写 `target` 与 `expected_found`；`expected_found=false` 的用例允许期望节点、关系与证据为空。

证据引用统一为 `table:row_id@version_id:tenant_id`。合成节点没有事实行 ID 时允许使用摘要型引用，但必须有同用例中的行级边证据支撑；禁止把合成节点写成拥有真实 `source_id`。`expected_truncated` 必须显式声明；`edges_from_seed_only=true` 只用于所有边都从种子出发的场景。

### 3.2 覆盖要求

gold 必须覆盖五种模式 `neighbors`、`path`、`usage`、`community`、`impact`，并覆盖全部九种关系类型。至少包含：正向路径、反向路径未命中、强文档边与弱文档禁止节点、有序模式相邻对、同簇兄弟与特征父链、文件级影响面、反向导入边、继承边、文档模式覆盖与一致性证据、页面级文档导航多块展开、诊断型歧义调用，以及明确的截断期望。

真实语料上的图查询质量不能由合成 gold 的全通过替代；新增真实用例时必须同时保留原始事实定位，并单独标注语料与版本。默认不把 gold 匹配率写成通用质量门禁；显式严格模式在用例为空或存在错误时必须失败。

### 3.3 审核输出

审核工具输出 JSON 与 Markdown 两份同源结果。Markdown 必须能独立复核：给出库、gold、作用域、查询参数、种子与期望种子、找到状态、节点与禁止节点、关系、期望与实际证据引用、边与节点证据可追溯率、合成节点、截断状态与错误。`unexpected_truncations` 只统计实际发生且未被期望的截断；查询错误单列，不混入截断指标。性能报告与 gold 匹配率分开呈现，缓存命中率不作为正确性指标。

## 4. 复核与验收

每批判定完成后逐项复核：题面能在原始正文定位；没有导航、页脚、脚本或截断片段；没有泛词实体；漂移题面含弃用语义；无答案题目标块无实体边；实体存在且可在题面点名；锚点、特征、判定与节点与派生证据一致；同一 URL 不跨 dev/test。图 gold 逐条核对 `source_ref`、期望 ID、关系类型、证据行、禁止节点与截断状态，并确认模式与关系类型覆盖完整。

同源一致性只证明标签与派生表一致，不替代独立语义复核。第三方复核完成前，文档特征增益权重保持为 0，不写检索增益门禁。每次变更都要同步更新人工核对记录、契约测试与用户文档中受影响的字段和参数说明；禁止保留被替换的旧条目作为兼容数据。
