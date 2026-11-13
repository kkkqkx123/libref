# 检索问答基准（真实项目夹具）

本目录提供一套离线、可重建、可对比的检索问答基准。语料不是人造样本，而是真实项目：
本仓库的 `docs/` 文档树喂文档分支，`src/libref/` 源码树喂源码分支，两条分支都走生产管线
（净化 → 双泳道分块 → 落库 → 全文索引；解析 → 分块 → 挖掘序列与聚类）。

数据集与报告都不入库：

- `data/`：`bench.db` 与 `manifest.json`，由 `build.py` 从夹具重建，随时可丢弃。
- `outputs/`：`report.md` 与 `results.json`，供人工审阅，断言不读写这些文件。

## 运行

```bash
uv run python -m tests.e2e.bench.build          # 重建数据集
uv run python -m tests.e2e.bench.evaluate       # 方法矩阵 + 专项探针 + 门禁
uv run python -m tests.e2e.bench.evaluate --no-probes   # 只跑方法矩阵
uv run python -m tests.e2e.bench.sweep          # 参数扫描（检索期 + 索引期分块）
LIBREF_RUN_BENCH=1 uv run pytest tests/integration/test_retrieval_bench.py -q
```

参数扫描的实测记录与设置结论见 `docs/archive/retrieval-bench-data.md`。

`evaluate` 退出码非零表示门禁不满足；报告里同时列出失败项与全部观测值。

## 文件职责

| 文件 | 职责 |
|---|---|
| `fixtures.json` | 夹具清单：文档树与源码树的位置、库名、版本；路径缺失时记为环境不可用 |
| `judgments.json` | 判定集：查询、类型（qualified/semantic/cjk/absent）、期望页面与可选期望小节 |
| `build.py` | 走生产管线灌库，产出数据集与清单（提交标识、页数、块数、模式与聚类规模） |
| `evaluate.py` | 方法矩阵、专项探针、门禁检查与报告生成 |
| `metrics.py` | 纯排序指标：Recall@k、MRR@k、nDCG@k、空结果率、重复率、分布统计 |
| `fakes.py` | 离线替身：词袋伪向量、基于 SQLite 真源的向量检索、词覆盖率重排、失败重排 |
| `gates.json` | 版本化门禁：全文路（生产代表性路径）的绝对下限 + 全方法的结构不变量 |
| `sweep.py` | 参数扫描：检索期旋钮复用数据集，索引期分块旋钮各自重建数据集 |

## 方法矩阵与门禁口径

- `precise`：生产全文单路，**代表性路径**，进入绝对下限门禁。
- `fused` / `fused_rerank`：生产融合与重排接线，向量与重排使用确定性替身。
  它们验证的是融合、同源去重、重排与降级行为，**绝对质量不代表真实语义模型**，
  因此只受结构不变量约束（结果不得出现重复材料）。
- 语义质量门禁需要真实嵌入快照（见下节）。

## 扩展

- **追加真实项目**：在 `fixtures.json` 增加条目即可（`kind` 为 `docs` 或 `code`，
  `path`/`source`/`subdir` 指向本地真实工程）。路径不存在时构建记入 `skipped`，
  评测直接以环境不可用退出，不当作回归。
- **判定集扩容**：`judgments.json` 的 `file` 用相对仓库根的路径锚定页面，`heading`
  用标题子串锚定小节；`absent` 类型用于观察空结果行为。
- **真实嵌入快照**：语义质量对比需要一次性录制（真实嵌入模型 + 真实查询向量）到
  `data/embeddings.json`，评测时用录制替身替换 `fakes.SqliteVecStore`。这是当前
  唯一缺少的环节，接入后 `fused*` 方可进入质量门禁。
- **回答级评估**：置信度与引用质量目前用回放用例（正确引用/错误引用/不引用）观测；
  接入真实模型回答录制后可扩展为回答级指标。
