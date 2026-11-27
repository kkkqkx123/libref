# 检索加权与结果增强边界调整

依据：`docs/plan/doc-feature-remaining-fixes.md`、冻结归档评测报告、`docs/tests/doc-feature-unblocked-analysis.md`，以及 `code-context-engine/crates/app/cce-orchestrator` 的查询后处理编排经验。参考项目在排序与阈值过滤之后只对 top-N 附加结构保持文本标注，排序阶段的关系加权已删除；本方案沿用这一职责边界。

## 一、问题清单

### 不应参与排序的路径

1. **文档块结构特征**：`example` 块 `+0.5`、`code_ratio>0` 加 `+0.25`、`deprecated` 扣 `0.5`，都是块自身属性，与查询无关。当前位于 `store/retriever/features.py::_doc_feature_bonuses`。
2. **文档实体对齐边**：`symbol` / `signature` / 弱边的累计加成同样不判断查询是否提到该实体；弱边虽然已从加成表移除，但强边加成仍是无查询条件的静态分。
3. **API 聚类/关系**：`api_clusters` 的簇命中通过 `cluster_alpha` 乘入分数，属于关系证据。参考项目已证明这类关系对排名无稳定收益，更适合做结果级文本增强。
4. **查询无关的过滤副作用**：`aligned_only` 可作为高精度检索模式保留，但不能作为默认增益；低强边覆盖站点会因此丢失大量候选。

### 仍保留的加权路径

1. `freq_alpha`：序列模式支持度，属于查询实体/正文可命中的复用频次证据。
2. `facet_alpha`：参数形态命中，属于查询实体可命中的用法证据。
3. RRF、rerank 与 FTS 权重不变。

### 评测与可观测性问题

1. 当前评测以 `doc_feature_alpha` 开关制造排序差分，但差分来自查询无关加成，不能证明增益。
2. `drift_samples.auto_verdict` 与被测规则同源，报告中的 0 误报/0 漏报不能作为独立语义结论。
3. 结果增强必须可关闭，且不能改变 `score`、名次和候选池。

## 二、修改方案

### 排序层

- 从 `HybridRetriever._assemble()` 的乘法项中移除文档块特征加成和聚类加成。
- 删除 `cluster_alpha`、`doc_feature_alpha` 两个配置字段及所有镜像；不再保留无效果的残留字段。
- `filter_by_doc_facets()` 保持现有显式过滤语义，不参与默认加权。

### 结果增强层

- 在排序、去重、过滤和窗口展开完成后，对 top-N 结果附加只读的派生注释：
  - 块类型与代码比例；
  - `deprecated` 状态；
  - 强对齐实体名；
  - 同簇成员名。
- 注释写入结果字段 `annotations`，不修改 `score`；搜索展示和问答材料在正文后追加带边界标记的文本。
- 注释数量由 `doc_feature_annotation_top_n` 控制；设为 0 时完全关闭。
- 弱边只保留在 `doc_insight` 诊断视图，不写注释、不加权。

### 评测层

- 冻结回放不再做 off/on nDCG/MRR 对比；排序不变性已由 `tests/integration/test_retriever.py` 覆盖，基准不重复测试。
- `evaluate_doc_features` 只跑一次带注释检索，导出逐题 raw/annotated Markdown，供人工审核。
- 报告保留派生证据质量、过滤行为和漂移抽检；删除排名归因与显著性差分。
- 漂移抽检保留人工判定；`auto_verdict` 由派生表 `doc_consistency` 回读，规则同源快照只用于复现，不写门禁。

## 三、验证口径

- 单测：审核导出包含 raw/annotated 对照与注释元数据。
- 集成测试：原有检索测试同时覆盖增强前后 ID、score、顺序一致及 top-N 边界，不新增重复用例。
- 冻结归档重放：报告不再输出 off/on nDCG，改为注释覆盖率、越界注释数与审核目录。
- `doc_feature_alpha=0` 的既有结论不反转，只是该字段被移除，不再存在“默认关闭但可开启”的加权通道。

## 六、复测结果

评测入口已改为逐题审核导出：`tests/e2e/bench/outputs/retrieval-bench/doc-features/annotation/` 下生成 `index.md` 与每站点、每题的 raw/annotated 对照；`REPORT.md` 只保留证据质量、过滤行为、漂移抽检与注释覆盖。排序不变性不再在基准中重复验证，仍由集成测试保证。

## 四、漂移评测独立性

`evaluate_doc_features._observed_drift_verdicts()` 在回放结束后按样本 URL、标题路径与版本回读 `doc_consistency`，把结果写入 `observed_auto_verdict`。报告只消费该字段；fixture 中的 `auto_verdict` 保留为历史观测快照，不再作为误报/漏报依据。

## 五、边界

- 不改双路召回、RRF、rerank、窗口展开与问答引用协议。
- 不补造实体、不提升弱边、不打开任何检索增益门禁。
- 真实嵌入链路仍缺失，结果增强只做文本可读性评估，不宣称质量增益。
