# 文档特征核查夹具

本目录为 `tests/e2e/render_doc_feature_outputs.py` 提供离线确定性输入，默认不联网、不访问向量后端与模型。

## 文件清单

- `cases.json`：库与版本、页面清单、API 实体、特征树节点、社区模式、下一版本覆盖页与可手算期望。
- `pages/*.md`、`pages/*.html`：固定正文。围栏示例调用同一 API 的两种顺序，参数表与返回值小节、废弃标记、变更日志、歧义短名与无实体页面各占一页。
- `pages_v11/*.md`：下一版本覆盖页，用于跨版本 facet 与实体变化清单。
- `questions.json`：人工确认后的判定集，按语料标识分组；离线 `demo` 场景与冻结归档场景共用固定字段。每题带来源、来源判定引用、题目、稳定锚点、期望实体、期望特征树节点、期望块特征、期望判定与分级。期望实体按子集命中评分，额外边单独记录；离线题目按期望节点集合完全相等评分。
- `drift_samples.json`：人工标注的漂移样本；自动 drift 与人工结论不一致时计入误报统计，样本不足时只报告数量，不写门禁。
- `../../fixtures/doc_features/golden.json`：纯算法黄金集，含实体、文档块、片段调用、对齐边、特征树父链、期望节点关联与模式；单元测试无需数据库即可复核。

## 生成与核查

```bash
uv run python tests/e2e/render_doc_feature_outputs.py
uv run pytest tests/integration/test_doc_feature_outputs.py -q
```

输出到 `tests/e2e/outputs/doc_features/`，按 `01` 至 `10` 分别核查输入、块 facet、对齐边、模式覆盖、一致性判定、锚点解析、排名对照、指标、跨版本变化与漂移复核。`08-metrics.txt` 同时记录仅示例、排除废弃、仅对齐三类过滤后的结果数、保留数与命中率。`SUMMARY.txt` 只汇总条数、锚点解析与不变量，人工判定不写进单元测试。

判定口径：稳定锚点只用 URL、标题路径、块分组标识与限定符号名；不用数据库 id、行号或块序号。相关等级沿用检索基准的强相关 2 分、一般相关 1 分、未命中 0 分。同一题目在 `doc_feature_alpha` 关闭与开启两列并排，数值差分与区间只作人工标定依据。

## 候选生成

`tests/e2e/generate_doc_feature_questions.py` 只读 `tests/e2e/bench/judgments.json` 产出候选：文档分支继承题目与 URL/标题锚点，源码分支从目标路径与题目抽取符号，只有同时具备文档锚点与源码符号的跨分支候选才标为可审。候选写到忽略的输出目录，需人工确认实体与块特征后才能进入 `questions.json`；单边证据保持草稿，不参与评分。

## 冻结归档层

归档需要显式下载，默认测试不依赖：

```bash
uv run python -m tests.e2e.bench.fetch --only httpx/docs --only httpx/code-7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016
uv run python -m tests.e2e.bench.build --fixture httpx/docs --fixture httpx/code-7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016
uv run python -m tests.e2e.bench.evaluate_doc_features
```

评测把归档原始正文中的围栏与 HTML 代码块登记为 `doc_snippet` 语料源，再按文档夹具的 `entity_source` 把文档版本显式绑定到源码版本，然后运行生产派生与读模型。代码块保留页面中的 import/using 上下文，供别名解析使用。报告写到 `tests/e2e/bench/outputs/retrieval-bench/doc-features/`，记录归档修订但不改门禁。缺少语言标记代码块的站点仍可核查规则匹配与检索加权，实体对齐率只作记录。
