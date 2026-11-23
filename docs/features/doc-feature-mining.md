# 库文档特征挖掘

库文档侧把 `pages` / `documents` / FTS 中的文本升格为带特征、可对齐、可度量的知识层。真源仍是文档正文、API 实体与调用实例；本文涉及的四张表全部是可重建派生表。

## 四层特征

- 结构特征：块类型、标题深度、长度分桶、围栏占比、表格与废弃标记，由规则从块正文中确定性地抽取。
- 内容特征：示例语言、示例符号、参数名、返回值提示、术语候选。示例符号来自文档示例片段已落库的 AST 调用证据，不做第二次解析。
- 对齐特征：文档块到 API 实体的关联边、文档块对挖掘模式的覆盖。关联优先级为 AST 精确调用、限定名命中、唯一短名归一命中；歧义名不建边。覆盖按保序子序列判定，与序列模式口径一致。
- 一致性特征：由废弃标记、关联边与覆盖结果的差集给出 `match` / `drift` / `unknown`；模型只在开启时给漂移样本写一句归纳说明，不改写判定。

## 派生表

| 表 | 粒度 | 作用 |
|---|---|---|
| doc_features | 版本 + 文档 + 特征面 | 结构与内容特征，值为统一的 `kind` 与 `details` 结构 |
| doc_api_links | 版本 + 文档 + 实体 | 对齐边，含匹配种类、置信度与证据 |
| doc_pattern_coverage | 档案 + 文档 + 模式哈希 + 覆盖种类 | 模式覆盖 |
| doc_consistency | 档案 + 文档 + 特征面 + 模型 + 提示版本 | 一致性结论与归纳说明 |

文档侧结构与实体对齐不依赖挖掘档案，按版本隔离；模式覆盖与一致性依赖档案，按档案隔离。整版清除顺序为一致性、覆盖、对齐边、结构特征，最后才是页面级联删除。

## 重建与触发

纯算法位于 `chunking/doc_facets.py` 与 `mining/doc_alignment.py`，均不访问数据库。编排入口是 `services/docs/feature_refresh.py: refresh_doc_features`：

- 文档抓取成功后重建 `structure` 与 `links`，并把关联实体写回 `documents.metadata.entities`；只有名单变化时才更新元数据，避免无谓触发 FTS。
- 模式挖掘成功后重建当前档案的 `coverage` 与 `consistency`。
- `libref doc-features refresh` 可独立重跑指定范围，任务名为 `derive_doc_features`，恢复语义与既有任务一致。
- 任一上游为空时清空对应派生范围；失败只计数告警，不推翻真源。

## 检索与出口

`doc_feature_alpha` 是第四个乘法加成项，默认零。加成来自含示例、已对齐实体与废弃标记的块级信号，并复用既有健康门控；块类型过滤、废弃排除、仅看对齐页是显式检索参数，精确路与混合路都支持。`doc_insight` 在 MCP、CLI 与 Web 三处提供 `features` / `links` / `coverage` / `consistency` 四视图；跨版本对比只读计算，不新增表。

配置、加权判定集与评估口径见 `docs/plan/doc-feature-mining.md`。
