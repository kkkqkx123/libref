# 文档特征判定集与漂移样本编写规范

本规范适用于 `tests/e2e/fixtures/doc_features/questions.json` 与 `tests/e2e/fixtures/doc_features/drift_samples.json`。冻结归档题目和漂移样本必须逐条手写；任何脚本只能列证据或在忽略目录中产出探索候选，不得生成、补全或回写最终判定。归档物化生成器已删除，不得重新引入。

## 1. 数据来源与职责

- `tests/e2e/bench/fixtures/*/docs/pages.jsonl.gz`：原始正文，只读。查看时解压到系统临时目录，不把解压结果写入版本库。题面语义以这里的正文句子为准。
- `tests/e2e/bench/data/bench.db`：派生证据。`documents.metadata.path` 提供标题路径，`doc_features` 提供 facet，`doc_api_links` 与 `api_entities` 提供实体，`doc_feature_node_links` 提供节点父链，`doc_consistency` 提供一致性判定。数据库只提供可核验事实，不提供题面文字。
- `questions.json`：最终判定集。每题包含目标 URL、题面、期望实体、块特征、判定、节点父链、id、划分与来源引用。当前基线为九个真实站点各 30 条、共 270 条，离线 `demo` 12 条不参与冻结归档改动。
- `drift_samples.json`：人工漂移样本。当前基线为 40 条，其中人工 drift 28 条、人工 not_drift 12 条；`auto_verdict` 只记录自动判定观察值，不能当真值。
- `tests/e2e/bench/judgments.json` 与 `generate_doc_feature_questions.py`：只作候选探索。候选池中的条目不得直接进入 `questions.json`；实体和块特征也不得从候选或词表自动填充。

## 2. 题目构造

### 2.1 选块

先确定题目要验证的文档块：从数据库取 URL、完整 `metadata.path`、块 facet、实体边、节点父链和一致性判定；再回到原始正文定位同一标题章节。同名标题存在多个块时，以数据库该行的完整路径和 facet 为准，不用页面第一处匹配兜底。题面、facet、实体和判定必须指向同一个块。

无合格正文证据的块不选；导航节点、页脚、目录列表、脚本或表格表头不作为题目来源。正文被归档截断后只剩菜单片段的块同样不选。

### 2.2 题面

题面由人根据章节原句写出，形式为自然语言问题。它应直接问出该句说明的事实，不使用“标题: 正文首句”拼接模板，不设固定字数上限，不在单词或数字中间截断。题目可以包含 API 名、参数名、版本号或代码标识符，但这些词必须来自该章节正文或该块的可核验派生事实。

题型按题面语义选择：示例、参数或返回、表格、变更说明、drift、无答案、限定名说明、普通语义题。不得仅因 facet 中出现数字就写变更题，也不得因 facet 中出现表格标记就把代码段写成表格题。

### 2.3 实体

实体只在三个条件同时满足时写入：题面明确点名该符号；目标文档的 `doc_api_links` 有该实体；源码版本绑定后的 `api_entities` 中存在该限定名。题面中只出现普通英文词且恰好与源码短名相同的不算点名，例如 `Warning`、`complete`、`configure`、`content`、`generate`、`sequence`、`wrapped`、`message`。

实体取题目实际询问的最小集合，不为了凑对齐率追加实体。题面没有合格实体时写空数组；实体必须能在题面里逐字找到限定名或短名。

### 2.4 标签与锚点

`expected_facets` 的块类型、标题深度、长度分桶、代码比例、表格标记、版本标记、示例语言、示例符号、参数名、返回值和废弃标记从派生表复制并人工核对；`expected_facets.heading` 只存 `metadata.path` 的末段，但作者必须同时核对完整路径。`source_ref` 摘要由语料标识、URL 和完整标题路径计算。

`expected_verdict` 只在题目确为 drift 且目标文档结论唯一时写入；其他题型写空。`expected_feature_nodes` 从目标文档的父链复制，节点不完整的题不写。无答案题必须选目标块没有 `doc_api_links` 的页面；题面里没有别名不能当作无答案依据。

`id` 保持语料前缀与两位序号；同一 URL 的题目必须全部落在同一 dev/test 划分。修改题面不改动 URL 与划分，除非该 URL 的证据本身不成立。

### 2.5 禁止项

禁止用生成器输出、正则首句、词表拼接、候选池 `ready` 标记或自动 drift 结论替代人工判断。禁止保留已被替换的旧题作为“兼容条目”，也不得为保持配额编造正文没有的题面。源文档或派生证据变化后，按本规范重写相关题目，不做旧格式兼容。

## 3. 漂移样本构造

### 3.1 真 drift

从原始正文定位包含 `deprecated`、`deprecation`、`removed`、`no longer` 或中文弃用、移除、替代说明的句子，确认它指向目标文档中具体 API、参数、字段、配置或行为。`human_verdict` 写 `drift`，`reason` 用一句话指出原句和替代关系，不能只写“命中 deprecated”。

`auto_verdict` 记录自动判定观察值；即使它与人工结论一致，也不能据此省略原文核对。题面中的 drift 必须包含同一弃用句的核心语义，不能只给标题或泛化描述。

### 3.2 人工 not_drift

以下情形写 `not_drift`，并在 `reason` 中说明为什么不是 API 漂移：

- 通用政策或版本支持说明，没有指向具体受跟踪 API。
- 否定式问答，例如“会被废弃吗？”正文回答不会。
- 生命周期状态枚举，例如 `Internal`、`Incubating`、`Public`、`Deprecated`。
- 变更枚举值，例如 `ADDED`、`REMOVED`、`MODIFIED`。
- 安全基线或外部标准，例如弃用的 TLS 版本、特殊地址段、第三方安全建议。
- 构建、测试、文档分类中的流程术语或日志文本。
- 只提到 breaking changes 但未给出具体弃用、移除或替代对象的泛化说明。

### 3.3 字段与覆盖

`corpus`、`url`、`heading`、`auto_verdict`、`human_verdict`、`reason` 必填。`url` 必须与数据库文档行完全一致；`heading` 使用完整 `metadata.path`，不用末段短标题替代。`reason` 必须可被原文复核。

样本应同时覆盖真实 drift 与人工 not_drift，并覆盖导航式目录、升级指南、迁移说明、生命周期枚举和安全基线等易误报形态。样本不足时只报告数量，不计算门禁误报率，也不修改 `gates.json`。

## 4. 复核与验收

每批手写完成后逐项复核：题面能在原始正文定位；没有导航、页脚、脚本或截断片段；没有泛词实体；drift 题面含弃用语义；无答案题目标块无实体边；实体存在且可在题面点名；锚点、facet、判定和节点与派生证据一致；同一 URL 不跨 dev/test。

冻结归档评测只证明标签与派生表一致，不替代语义复核。独立第三方复核完成前，`doc_feature_alpha` 保持 0，不写检索增益门禁。每次变更都要同步更新人工核对记录中的发现、处置与复核结果。
